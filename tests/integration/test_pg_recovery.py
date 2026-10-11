"""Day 6 E: the UNKNOWN-refund recovery loop on real PostgreSQL.

Every test runs the production store (``PgRefundStore``) and the real
recovery service over the dedicated test database:

- discovery across "restarts" (fresh wiring re-reads only the
  database);
- gateway SUCCEEDED settles exactly once, FAILED releases the
  reservation only on definitive evidence, everything else keeps the
  refund unknown with the reservation held;
- the lease: atomic claim, expiry-after-crash recovery, and the owner
  token blocking a late worker from overwriting newer state;
- the crash window: a PENDING row whose gateway executed before the
  application died;
- audit/accounting atomicity under a forced audit failure.

The gateway is the deterministic :class:`FakeGateway` double — queries
are simulated, the database behavior is real.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.domain.enums import IssueType, RefundStatus
from app.domain.models import Refund
from app.persistence.models import AuditEventRow, OrderRow, RefundRow
from app.persistence.pg_catalog import ensure_catalog_seeded
from app.persistence.pg_refund_store import PgRefundStore
from app.services.payments import FakeGateway, GatewayOutcome
from app.services.recovery import RecoveryService

ORDER_ID = "ORD-1001"  # refundable: total 199.99
TICKET_ID = "TKT-2001"
AMOUNT = Decimal("50.00")
# Import-time "now": real clock, so PENDING grace windows compare against
# the present (a fixed constant could land in the future and silently
# exclude rows the test means to recover).
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def seeded(pg_engine):
    ensure_catalog_seeded(sessionmaker(bind=pg_engine, expire_on_commit=False))


def _sessions(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


def _session(pg_engine):
    return _sessions(pg_engine)()


def _create_refund(pg_engine, *, key: str, status: RefundStatus | None = None,
                   amount: Decimal = AMOUNT) -> Refund:
    """Create a refund via the real store; optionally leave it unresolved
    (PENDING) or move it to UNKNOWN via the real finalize path."""
    store = PgRefundStore(_sessions(pg_engine))
    outcome = store.create(
        None,
        order_id=ORDER_ID,
        amount=amount,
        idempotency_key=key,
        ticket_id=TICKET_ID,
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=None,
        run_id=None,
        reason="recovery integration test",
        ticket_claimed_amount=Decimal("199.99"),
        now=NOW,
    )
    assert outcome.kind == "execute", outcome.kind
    refund = outcome.refund
    if status is RefundStatus.UNKNOWN:
        refund = store.finalize(
            refund.refund_id, RefundStatus.UNKNOWN, orders=None, now=NOW
        )
    return refund


def _order(pg_engine) -> OrderRow:
    with _session(pg_engine) as s:
        return s.get(OrderRow, ORDER_ID)


def _refund_row(pg_engine, refund_id: str) -> RefundRow:
    with _session(pg_engine) as s:
        return s.get(RefundRow, refund_id)


def _expire_claims(pg_engine) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE refunds SET recovery_claim_expires_at = "
                "now() - interval '1 second' "
                "WHERE recovery_claim_expires_at IS NOT NULL"
            )
        )


def _recovery(pg_engine, gateway, **kwargs) -> RecoveryService:
    return RecoveryService(
        PgRefundStore(_sessions(pg_engine)), gateway, **kwargs
    )


def _run(service) -> "RecoverySummary":
    return asyncio.run(service.recover_once())


# ---------------------------------------------------------------------
# discovery and the three gateway evidence classes
# ---------------------------------------------------------------------


def test_unknown_refund_is_discovered_and_settled_on_succeeded_evidence(
    pg_engine, seeded
):
    refund = _create_refund(pg_engine, key="rec-1", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})

    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.discovered == 1
    assert summary.settled_processed == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "processed"
    assert order.refunded_amount == AMOUNT  # settled exactly once
    assert order.reserved_amount == Decimal("0")
    assert row.recovery_attempts == 1
    assert row.recovery_claim_owner is None  # lease cleared on settle
    assert row.last_recovery_error is None


def test_second_recovery_run_does_not_settle_twice(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-2", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})
    service = _recovery(pg_engine, gateway, pending_grace_seconds=0)

    first = _run(service)
    second = _run(service)

    assert first.settled_processed == 1
    assert second.discovered == 0  # the row is terminal: never re-claimed
    order = _order(pg_engine)
    assert order.refunded_amount == AMOUNT  # the money did not move twice
    assert gateway.query_calls == 1


def test_failed_evidence_releases_reservation(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-3", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.REJECTED})

    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.settled_failed == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "failed"
    assert order.reserved_amount == Decimal("0")  # released
    assert order.refunded_amount == Decimal("0")  # nothing settled


def test_unknown_evidence_keeps_status_and_reservation(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-4", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.UNKNOWN})

    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.kept_unknown == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "unknown"  # never auto-FAILED
    assert order.reserved_amount == AMOUNT  # reservation kept, fail closed
    assert row.last_recovery_error == (
        "gateway could not determine the outcome (unknown)"
    )


def test_query_error_never_becomes_failed(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-5", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(query_errors={refund.refund_id: "connection reset"})

    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.kept_unknown == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "unknown"  # a timeout is NOT evidence of failure
    assert order.reserved_amount == AMOUNT
    assert "connection reset" in row.last_recovery_error


# ---------------------------------------------------------------------
# lease: retry schedule, crash takeover, stale-worker protection
# ---------------------------------------------------------------------


def test_active_claim_blocks_recovery_and_expiry_reenables(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-6", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.UNKNOWN})
    service = _recovery(
        pg_engine, gateway, lease_seconds=60.0, pending_grace_seconds=0
    )

    first = _run(service)
    second = _run(service)  # lease still valid: nothing to recover

    assert first.kept_unknown == 1
    assert second.discovered == 0
    row = _refund_row(pg_engine, refund.refund_id)
    assert row.recovery_attempts == 1  # the claim is the retry schedule

    _expire_claims(pg_engine)
    third = _run(service)
    assert third.kept_unknown == 1  # re-queried after expiry
    row = _refund_row(pg_engine, refund.refund_id)
    assert row.recovery_attempts == 2
    assert gateway.query_calls == 2


def test_crashed_worker_claim_recovers_after_lease_expiry(pg_engine, seeded):
    """A worker claims, then dies (never settles). The lease expires and
    a new worker settles from the same gateway evidence."""
    refund = _create_refund(pg_engine, key="rec-7", status=RefundStatus.UNKNOWN)
    store = PgRefundStore(_sessions(pg_engine))
    claimed = store.claim_recovery(
        refund.refund_id,
        owner="worker-A",
        expires_at=NOW + timedelta(seconds=30),
        now=NOW,
    )
    assert claimed == "claimed"

    _expire_claims(pg_engine)  # worker-A crashed; its lease lapses
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})
    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.settled_processed == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "processed"
    assert order.refunded_amount == AMOUNT


def test_stale_worker_cannot_overwrite_newer_state(pg_engine, seeded):
    """Worker A queries SUCCEEDED but is slow; its lease lapses, worker B
    takes over and keeps the refund UNKNOWN (gateway evidence changed).
    A's late settle must be refused by the owner token — B's state
    stands, nothing is written twice."""
    refund = _create_refund(pg_engine, key="rec-8", status=RefundStatus.UNKNOWN)
    store = PgRefundStore(_sessions(pg_engine))
    store.claim_recovery(
        refund.refund_id,
        owner="worker-A",
        expires_at=NOW + timedelta(seconds=30),
        now=NOW,
    )
    gateway_a = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})
    assert asyncio.run(gateway_a.query_status(refund.refund_id)).value == "succeeded"

    _expire_claims(pg_engine)  # A stalls; the lease moves on
    gateway_b = FakeGateway(ledger={refund.refund_id: GatewayOutcome.UNKNOWN})
    summary = _run(_recovery(pg_engine, gateway_b, pending_grace_seconds=0))
    assert summary.kept_unknown == 1  # B now owns the row

    late = store.settle_recovered(
        refund.refund_id,
        expected_owner="worker-A",
        status=RefundStatus.PROCESSED,
        now=NOW,
        gateway_result="succeeded",
    )
    assert late == "lost_ownership"
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "unknown"  # A's stale result did not land
    assert order.refunded_amount == Decimal("0")
    assert order.reserved_amount == AMOUNT


# ---------------------------------------------------------------------
# concurrency and the crash window
# ---------------------------------------------------------------------


def test_two_concurrent_workers_settle_exactly_once(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-9", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})
    barrier = threading.Barrier(2)
    summaries: list = []

    def worker() -> None:
        service = _recovery(pg_engine, gateway, pending_grace_seconds=0)
        barrier.wait()
        summaries.append(asyncio.run(service.recover_once()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    settled = sum(s.settled_processed for s in summaries)
    assert settled == 1  # one worker settled, the other saw the claim
    order = _order(pg_engine)
    assert order.refunded_amount == AMOUNT  # never doubled
    assert order.reserved_amount == Decimal("0")


def test_crash_window_pending_row_is_recovered(pg_engine, seeded):
    """The gateway executed successfully and the application died before
    finalize: the row is still PENDING. Recovery queries the original
    operation (never executes a new one) and settles once."""
    refund = _create_refund(pg_engine, key="rec-10")  # PENDING, crashed
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})

    summary = _run(_recovery(pg_engine, gateway, pending_grace_seconds=0))

    assert summary.settled_processed == 1
    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "processed"
    assert order.refunded_amount == AMOUNT
    assert order.reserved_amount == Decimal("0")
    assert gateway.execute_calls == 0  # recovery never executed anything


def test_pending_row_within_grace_is_left_to_its_executor(pg_engine, seeded):
    refund = _create_refund(pg_engine, key="rec-11")  # PENDING, live owner
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})

    summary = _run(
        _recovery(pg_engine, gateway, pending_grace_seconds=3600.0)
    )

    assert summary.discovered == 0  # grace protects the live executor
    assert gateway.query_calls == 0


def test_repeated_unknown_never_re_executes(pg_engine, seeded):
    """Long-term unknown: repeated recovery passes re-query the same
    operation and never submit a new refund."""
    refund = _create_refund(pg_engine, key="rec-12", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.UNKNOWN})
    service = _recovery(
        pg_engine, gateway, lease_seconds=1.0, pending_grace_seconds=0
    )

    for _ in range(3):
        summary = _run(service)
        assert summary.kept_unknown == 1
        _expire_claims(pg_engine)

    row = _refund_row(pg_engine, refund.refund_id)
    order = _order(pg_engine)
    assert row.status == "unknown"
    assert order.reserved_amount == AMOUNT  # still held, fail closed
    assert gateway.execute_calls == 0  # three passes, zero executions
    assert gateway.query_calls == 3
    assert row.recovery_attempts == 3


# ---------------------------------------------------------------------
# audit integrity
# ---------------------------------------------------------------------


def test_recovery_settle_writes_audit_and_failure_rolls_back_atomically(
    pg_engine, seeded, monkeypatch
):
    refund = _create_refund(pg_engine, key="rec-13", status=RefundStatus.UNKNOWN)
    gateway = FakeGateway(ledger={refund.refund_id: GatewayOutcome.PROCESSED})
    service = _recovery(pg_engine, gateway, pending_grace_seconds=0)

    summary = _run(service)
    assert summary.settled_processed == 1
    with _session(pg_engine) as s:
        settle_events = [
            row
            for row in s.execute(
                select(AuditEventRow).where(
                    AuditEventRow.action == "refund_store.recovery_settle"
                )
            ).scalars()
            if row.details.get("refund_id") == refund.refund_id
        ]
        # Every audit event naming a refund references an existing row.
        orphans = s.execute(
            text(
                "SELECT COUNT(*) FROM audit_events a "
                "WHERE a.details->>'refund_id' IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM refunds r "
                "WHERE r.refund_id = a.details->>'refund_id')"
            )
        ).scalar_one()
    assert len(settle_events) == 1
    assert settle_events[0].details["gateway_result"] == "succeeded"
    assert orphans == 0

    # A second UNKNOWN refund whose settle audit write fails must roll
    # back completely: no state change, no reservation move, no orphan
    # audit row.
    other = _create_refund(pg_engine, key="rec-14", status=RefundStatus.UNKNOWN)
    store = PgRefundStore(_sessions(pg_engine))
    # Take the lease first so the settle passes the ownership gate and
    # reaches the audit write.
    store.claim_recovery(
        other.refund_id,
        owner="worker",
        expires_at=NOW + timedelta(seconds=30),
        now=NOW,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(store, "_audit_row", boom)
    with pytest.raises(RuntimeError, match="audit write failed"):
        store.settle_recovered(
            other.refund_id,
            expected_owner="worker",
            status=RefundStatus.PROCESSED,
            now=NOW,
            gateway_result="succeeded",
        )
    row = _refund_row(pg_engine, other.refund_id)
    order = _order(pg_engine)
    assert row.status == "unknown"
    assert order.reserved_amount == AMOUNT  # the reservation survived
    assert order.refunded_amount == AMOUNT  # only the first refund's money
