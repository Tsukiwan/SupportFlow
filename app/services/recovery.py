"""UNKNOWN-refund recovery service (Day 6).

Closes the loop Day 5 left open: a refund whose gateway outcome is
unknown keeps its reservation forever unless something settles it. This
service re-discovers such refunds after an application restart, queries
the gateway for the definitive state of the *original* operation (never
executing a new one), and settles the local record from the evidence.

Design rules this module follows:

- recovery is read-only against the gateway: it only ever calls
  ``query_status``, never ``execute``. There is no execute path here by
  construction — a bug in this module cannot double-charge;
- the gateway is asked about the stable refund id persisted on the row
  before any execute call, so the query always targets the original
  operation, including one the application crashed in the middle of;
- a database transaction is never held across the gateway query. The
  claim is one atomic conditional UPDATE (a lease with an owner token
  and an expiry); the query runs outside any transaction; the settle is
  a second transaction guarded by a re-check of the lease under a row
  lock. A late worker whose lease expired cannot overwrite a newer
  worker's state;
- only gateway evidence moves money: SUCCEEDED settles PROCESSED,
  FAILED settles FAILED (releasing the reservation), and everything else
  — timeouts, query errors, still processing, index delay, no record —
  keeps the refund UNKNOWN with the reservation held and a recorded
  reason. Expiry of a lease only permits another query, never another
  execution;
- the ordinary database transaction and the external gateway call are
  NOT atomic, and nothing here pretends otherwise: the same-op query and
  the idempotent, lock-guarded settle are what keep the window safe.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import uuid4

from app.domain.enums import RefundStatus
from app.domain.models import Refund
from app.persistence.pg_refund_store import PgRefundStore
from app.services.payments import GatewayQueryResult, RefundStatusQuery

logger = logging.getLogger(__name__)


class RecoveryOutcome(str, Enum):
    """What one recovery attempt did to one refund."""

    SETTLED_PROCESSED = "settled_processed"  # gateway evidence: executed
    SETTLED_FAILED = "settled_failed"  # gateway evidence: finally failed
    KEPT_UNKNOWN = "kept_unknown"  # no evidence either way; reservation kept
    ALREADY_TERMINAL = "already_terminal"  # settled by someone else meanwhile
    LOST_OWNERSHIP = "lost_ownership"  # lease moved on; our result discarded
    CLAIM_UNAVAILABLE = "claim_unavailable"  # another lease active, or not found


@dataclass(frozen=True)
class RecoveryResult:
    """Observable outcome of one attempt against one refund."""

    refund_id: str
    outcome: RecoveryOutcome
    gateway_result: GatewayQueryResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class RecoverySummary:
    """Observable summary of one ``recover_once`` pass."""

    discovered: int
    settled_processed: int
    settled_failed: int
    kept_unknown: int
    already_terminal: int
    lost_ownership: int
    claim_unavailable: int
    results: tuple[RecoveryResult, ...]

    @property
    def claimed(self) -> int:
        """Attempts that actually took the lease and queried the gateway."""
        return (
            self.settled_processed
            + self.settled_failed
            + self.kept_unknown
            + self.already_terminal
            + self.lost_ownership
        )


def plan_recovery_action(
    status: RefundStatus, result: GatewayQueryResult
) -> RefundStatus | None:
    """Pure decision: which target status does gateway evidence justify?

    Returns the settle target (PROCESSED/FAILED), or None when nothing
    may be written (still unknown, or the row is already terminal).
    Terminal rows are never moved regardless of the query result — a
    newer state always wins."""
    if status not in (RefundStatus.PENDING, RefundStatus.UNKNOWN):
        return None
    if result is GatewayQueryResult.SUCCEEDED:
        return RefundStatus.PROCESSED
    if result is GatewayQueryResult.FAILED:
        return RefundStatus.FAILED
    return None  # UNKNOWN: keep the reservation, keep the status


class RecoveryService:
    """Minimal UNKNOWN-refund recovery loop over the PostgreSQL store.

    Requires a gateway with a status-query capability (refuses to
    construct otherwise — recovering without one would be guessing). The
    in-memory store has no persistence across restarts, so recovery is a
    PostgreSQL-backend concern and this service takes
    :class:`~app.persistence.pg_refund_store.PgRefundStore` directly.

    ``lease_seconds`` bounds how long one worker may hold a claim (its
    expiry is the retry schedule for rows that stay unknown);
    ``pending_grace_seconds`` keeps a freshly-created PENDING row out of
    the scan while a live executor's gateway call may still be in flight
    (safety never depends on this window — it only avoids wasted
    queries). Explicit ``recover_refund`` calls bypass the grace window
    but keep every other guard."""

    def __init__(
        self,
        store: PgRefundStore,
        gateway: RefundStatusQuery,
        *,
        lease_seconds: float = 120.0,
        pending_grace_seconds: float = 60.0,
    ) -> None:
        if not callable(getattr(gateway, "query_status", None)):
            raise TypeError(
                "RecoveryService requires a gateway with a status-query "
                "capability (query_status(refund_id)); refusing to recover "
                "blindly without one"
            )
        self._store = store
        self._gateway = gateway
        self._lease_seconds = lease_seconds
        self._pending_grace_seconds = pending_grace_seconds

    async def recover_once(self) -> RecoverySummary:
        """Scan every eligible refund and attempt recovery on each.

        Safe to run concurrently from several workers: the claim UPDATE
        is atomic, and the settle is lease-guarded, so duplicate queries
        at worst duplicate read-only gateway calls — never money."""
        now = datetime.now(timezone.utc)
        candidates = self._store.find_recoverable(
            now, pending_grace_seconds=self._pending_grace_seconds
        )
        results: list[RecoveryResult] = []
        for refund in candidates:
            results.append(await self._recover_one(refund, now))
        return self._summarize(len(candidates), results)

    async def recover_refund(self, refund_id: str) -> RecoveryResult:
        """Recovery attempt against one explicit refund id. Bypasses the
        PENDING grace window (an operator asked for this row) but keeps
        the claim and settle guards."""
        refund = self._store.get(refund_id)
        if refund is None:
            return RecoveryResult(
                refund_id, RecoveryOutcome.CLAIM_UNAVAILABLE, error="not found"
            )
        return await self._recover_one(refund, datetime.now(timezone.utc))

    async def _recover_one(self, refund: Refund, now: datetime) -> RecoveryResult:
        owner = uuid4().hex
        claim = self._store.claim_recovery(
            refund.refund_id,
            owner=owner,
            expires_at=now + timedelta(seconds=self._lease_seconds),
            now=now,
        )
        if claim == "terminal":
            return RecoveryResult(refund.refund_id, RecoveryOutcome.ALREADY_TERMINAL)
        if claim == "active_claim":
            return RecoveryResult(
                refund.refund_id,
                RecoveryOutcome.CLAIM_UNAVAILABLE,
                error="recovery claim held by another worker",
            )
        if claim == "not_found":
            return RecoveryResult(
                refund.refund_id, RecoveryOutcome.CLAIM_UNAVAILABLE, error="not found"
            )

        try:
            gateway_result = await self._gateway.query_status(refund.refund_id)
            error: str | None = None
        except Exception as exc:  # query failure says nothing about the refund
            logger.warning(
                "gateway status query failed during recovery refund_id=%s: %s",
                refund.refund_id,
                exc,
            )
            gateway_result = GatewayQueryResult.UNKNOWN
            error = f"gateway status query failed: {exc}"

        target = plan_recovery_action(RefundStatus(refund.status), gateway_result)
        if target is not None:
            settle = self._store.settle_recovered(
                refund.refund_id,
                expected_owner=owner,
                status=target,
                now=datetime.now(timezone.utc),
                gateway_result=gateway_result.value,
            )
            outcome = {
                "settled": (
                    RecoveryOutcome.SETTLED_PROCESSED
                    if target is RefundStatus.PROCESSED
                    else RecoveryOutcome.SETTLED_FAILED
                ),
                "already_terminal": RecoveryOutcome.ALREADY_TERMINAL,
                "lost_ownership": RecoveryOutcome.LOST_OWNERSHIP,
                "not_found": RecoveryOutcome.CLAIM_UNAVAILABLE,
            }[settle]
            return RecoveryResult(
                refund.refund_id, outcome, gateway_result=gateway_result
            )
        kept = self._store.mark_recovery_kept_unknown(
            refund.refund_id,
            expected_owner=owner,
            reason=error or f"gateway could not determine the outcome ({gateway_result.value})",
            now=datetime.now(timezone.utc),
        )
        kept_outcome = {
            "recorded": RecoveryOutcome.KEPT_UNKNOWN,
            "already_terminal": RecoveryOutcome.ALREADY_TERMINAL,
            "lost_ownership": RecoveryOutcome.LOST_OWNERSHIP,
            "not_found": RecoveryOutcome.CLAIM_UNAVAILABLE,
        }[kept]
        return RecoveryResult(
            refund.refund_id, kept_outcome, gateway_result=gateway_result, error=error
        )

    @staticmethod
    def _summarize(
        discovered: int, results: list[RecoveryResult]
    ) -> RecoverySummary:
        counters = {
            RecoveryOutcome.SETTLED_PROCESSED: 0,
            RecoveryOutcome.SETTLED_FAILED: 0,
            RecoveryOutcome.KEPT_UNKNOWN: 0,
            RecoveryOutcome.ALREADY_TERMINAL: 0,
            RecoveryOutcome.LOST_OWNERSHIP: 0,
            RecoveryOutcome.CLAIM_UNAVAILABLE: 0,
        }
        for result in results:
            counters[result.outcome] += 1
        return RecoverySummary(
            discovered=discovered,
            settled_processed=counters[RecoveryOutcome.SETTLED_PROCESSED],
            settled_failed=counters[RecoveryOutcome.SETTLED_FAILED],
            kept_unknown=counters[RecoveryOutcome.KEPT_UNKNOWN],
            already_terminal=counters[RecoveryOutcome.ALREADY_TERMINAL],
            lost_ownership=counters[RecoveryOutcome.LOST_OWNERSHIP],
            claim_unavailable=counters[RecoveryOutcome.CLAIM_UNAVAILABLE],
            results=tuple(results),
        )
