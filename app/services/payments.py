"""Simulated payment gateway.

Day 2 simulates refunds end to end without touching a real payment
platform. The gateway is the single integration point that can produce
temporary failures, rejections or unknown outcomes; everything else in
the refund pipeline is deterministic.

``SimulatedPaymentGateway`` yields once per call (``asyncio.sleep(0)``) so
concurrent requests can interleave, which exercises the in-flight
protections of the refund store in tests.

Day 6 adds the *post-hoc status query* side of the gateway contract
(:class:`GatewayQueryResult` / :class:`RefundStatusQuery`) used by the
recovery service, plus :class:`FakeGateway` — a stateful, addressable
gateway double for recovery tests and the crash-recovery demo. The
double keeps a ledger of executed operations keyed by the refund id, so
a later query answers definitively even across an application restart.
It is NOT a real payment gateway and is never wired in place of one.
"""

import asyncio
from collections.abc import Callable
from enum import Enum
from typing import Protocol

from app.domain.models import Refund


class GatewayOutcome(str, Enum):
    PROCESSED = "processed"
    REJECTED = "rejected"
    TRANSIENT_FAILURE = "transient_failure"
    UNKNOWN = "unknown"


class GatewayQueryResult(str, Enum):
    """A post-hoc status query answer about one refund operation.

    Exactly three classes, by contract:

    - ``SUCCEEDED``: the gateway has definitive evidence the operation
      executed and the money moved;
    - ``FAILED``: the gateway has definitive evidence the operation
      finally failed and no identical operation is still processing;
    - ``UNKNOWN``: the gateway cannot determine the outcome — timeouts,
      temporary unavailability, query errors, still processing, index
      delay, or insufficient evidence. Anything that is not definitive
      proof in either direction MUST be reported as UNKNOWN.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class GatewayQueryError(Exception):
    """The status query itself failed (timeout, transport error, gateway
    unavailable). It says nothing about the refund outcome; callers must
    treat the operation as still UNKNOWN."""


class RefundStatusQuery(Protocol):
    """The recovery-facing side of the gateway contract: answer the
    definitive state of the operation identified by ``refund_id`` (the
    stable identifier persisted on the refund row before any execute
    call). Never executes anything — a status query is read-only."""

    async def query_status(self, refund_id: str) -> GatewayQueryResult: ...


class PaymentGateway(Protocol):
    async def execute(self, refund: Refund) -> GatewayOutcome: ...

    async def status(self, refund_id: str) -> GatewayOutcome: ...


class SimulatedPaymentGateway:
    """Pops one outcome per call from a queue; defaults to PROCESSED.

    ``execute_calls`` / ``status_calls`` count gateway invocations so tests
    can prove a recovery path queries the original operation instead of
    blindly executing a second refund."""

    def __init__(self, outcomes: list[GatewayOutcome] | None = None) -> None:
        self._queue: list[GatewayOutcome] = list(outcomes or [])
        self.calls = 0
        self.execute_calls = 0
        self.status_calls = 0

    def _next(self) -> GatewayOutcome:
        self.calls += 1
        if self._queue:
            return self._queue.pop(0)
        return GatewayOutcome.PROCESSED

    async def execute(self, refund: Refund) -> GatewayOutcome:
        self.execute_calls += 1
        await asyncio.sleep(0)
        return self._next()

    async def status(self, refund_id: str) -> GatewayOutcome:
        self.status_calls += 1
        await asyncio.sleep(0)
        return self._next()


class FakeGateway:
    """Deterministic in-process gateway double for recovery tests and the
    crash-recovery demo. NOT a real payment gateway — real-gateway
    behavior is unverified and this class must never be wired where a
    real gateway belongs.

    The gateway is an *external* system from the application's point of
    view: ``execute`` records the operation outcome in a ledger keyed by
    the refund id, and that record is what a later ``query_status`` reads
    — so the simulation keeps answering definitively after the
    application process dies between ``execute`` and ``finalize``.

    - An executed refund id answers with its recorded outcome mapped to
      the three query classes (PROCESSED → SUCCEEDED; REJECTED and
      TRANSIENT_FAILURE → FAILED, both "definitively nothing processing"
      under the simulated contract; UNKNOWN → UNKNOWN).
    - An unknown refund id answers UNKNOWN (the operation never landed,
      or the index has not caught up) — never FAILED.
    - ``query_errors[refund_id]`` makes the query raise
      :class:`GatewayQueryError` (timeout/transport simulation).
    - Re-executing an already-recorded refund id returns the recorded
      outcome without moving money twice.
    """

    def __init__(
        self,
        *,
        ledger: dict[str, GatewayOutcome] | None = None,
        execution_policy: Callable[[Refund], GatewayOutcome] | None = None,
        query_errors: dict[str, str] | None = None,
        query_delay: float = 0.0,
    ) -> None:
        self._ledger: dict[str, GatewayOutcome] = dict(ledger or {})
        self._policy = execution_policy or (lambda _r: GatewayOutcome.PROCESSED)
        self._query_errors: dict[str, str] = dict(query_errors or {})
        self._query_delay = query_delay
        self.execute_calls = 0
        self.query_calls = 0

    async def execute(self, refund: Refund) -> GatewayOutcome:
        """Record-and-return, idempotent per refund id: the same operation
        never moves money twice in this simulation."""
        self.execute_calls += 1
        recorded = self._ledger.get(refund.refund_id)
        if recorded is None:
            recorded = self._policy(refund)
            self._ledger[refund.refund_id] = recorded
        await asyncio.sleep(0)
        return recorded

    async def query_status(self, refund_id: str) -> GatewayQueryResult:
        self.query_calls += 1
        if self._query_delay:
            await asyncio.sleep(self._query_delay)
        error = self._query_errors.get(refund_id)
        if error is not None:
            raise GatewayQueryError(error)
        recorded = self._ledger.get(refund_id)
        if recorded is None:
            return GatewayQueryResult.UNKNOWN
        return {
            GatewayOutcome.PROCESSED: GatewayQueryResult.SUCCEEDED,
            GatewayOutcome.REJECTED: GatewayQueryResult.FAILED,
            GatewayOutcome.TRANSIENT_FAILURE: GatewayQueryResult.FAILED,
            GatewayOutcome.UNKNOWN: GatewayQueryResult.UNKNOWN,
        }[recorded]
