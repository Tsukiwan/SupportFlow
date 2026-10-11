"""Day 6 unit tests: recovery decision logic, the gateway query mapping
and the recovery service's flow control — all without a database.

The PostgreSQL claim/settle semantics are exercised for real in
``tests/integration/test_pg_recovery.py``; the stub store here exists
only to drive the service's control flow (error handling, ownership
races, summary accounting) quickly and deterministically. It is a test
double for the store surface, not a second state machine: every state
rule it mimics is re-verified against the real store in the integration
suite.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import IssueType, RefundStatus
from app.domain.models import Refund
from app.services.payments import (
    FakeGateway,
    GatewayOutcome,
    GatewayQueryError,
    GatewayQueryResult,
)
from app.services.recovery import (
    RecoveryOutcome,
    RecoveryService,
    plan_recovery_action,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _refund(refund_id: str, status: RefundStatus) -> Refund:
    return Refund(
        refund_id=refund_id,
        idempotency_key=f"key-{refund_id}",
        ticket_id="TKT-2001",
        order_id="ORD-1001",
        amount=Decimal("50.00"),
        currency="USD",
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=None,
        run_id=None,
        status=status,
        reason="unit test reason",
        created_at=NOW,
        processed_at=None,
    )


# ---------------------------------------------------------------------
# decision logic: only gateway evidence moves money
# ---------------------------------------------------------------------


def test_unknown_with_succeeded_evidence_settles_processed():
    assert (
        plan_recovery_action(RefundStatus.UNKNOWN, GatewayQueryResult.SUCCEEDED)
        is RefundStatus.PROCESSED
    )


def test_unknown_with_failed_evidence_settles_failed():
    assert (
        plan_recovery_action(RefundStatus.UNKNOWN, GatewayQueryResult.FAILED)
        is RefundStatus.FAILED
    )


def test_unknown_evidence_never_moves_money():
    assert plan_recovery_action(RefundStatus.UNKNOWN, GatewayQueryResult.UNKNOWN) is None


def test_crash_window_pending_with_succeeded_settles():
    # A PENDING row whose gateway already executed (crash before finalize)
    # must settle — this is the whole point of the recovery loop.
    assert (
        plan_recovery_action(RefundStatus.PENDING, GatewayQueryResult.SUCCEEDED)
        is RefundStatus.PROCESSED
    )


def test_pending_with_unknown_evidence_stays():
    assert plan_recovery_action(RefundStatus.PENDING, GatewayQueryResult.UNKNOWN) is None


@pytest.mark.parametrize(
    "status",
    [RefundStatus.PROCESSED, RefundStatus.FAILED, RefundStatus.REJECTED],
)
def test_terminal_rows_are_never_moved_regardless_of_evidence(status):
    for result in GatewayQueryResult:
        assert plan_recovery_action(status, result) is None


# ---------------------------------------------------------------------
# fake gateway: the three-class query contract
# ---------------------------------------------------------------------


def test_fake_gateway_maps_recorded_outcomes():
    gateway = FakeGateway(
        ledger={
            "REF-A": GatewayOutcome.PROCESSED,
            "REF-B": GatewayOutcome.REJECTED,
            "REF-C": GatewayOutcome.TRANSIENT_FAILURE,
            "REF-D": GatewayOutcome.UNKNOWN,
        }
    )
    import asyncio

    assert (
        asyncio.run(gateway.query_status("REF-A")) is GatewayQueryResult.SUCCEEDED
    )
    assert asyncio.run(gateway.query_status("REF-B")) is GatewayQueryResult.FAILED
    assert asyncio.run(gateway.query_status("REF-C")) is GatewayQueryResult.FAILED
    assert asyncio.run(gateway.query_status("REF-D")) is GatewayQueryResult.UNKNOWN


def test_fake_gateway_unknown_id_is_unknown_never_failed():
    """An id the gateway never saw is UNKNOWN (index delay, never landed)
    — not FAILED. Recovery must not release a reservation on it."""
    import asyncio

    gateway = FakeGateway()
    assert (
        asyncio.run(gateway.query_status("REF-GHOST"))
        is GatewayQueryResult.UNKNOWN
    )


def test_fake_gateway_query_errors_raise():
    import asyncio

    gateway = FakeGateway(query_errors={"REF-X": "connection reset"})
    with pytest.raises(GatewayQueryError, match="connection reset"):
        asyncio.run(gateway.query_status("REF-X"))


def test_fake_gateway_execute_is_idempotent_per_refund_id():
    """Re-executing the same refund id returns the recorded outcome — the
    simulation never moves the money twice."""
    import asyncio

    gateway = FakeGateway(execution_policy=lambda r: GatewayOutcome.PROCESSED)
    refund = _refund("REF-1", RefundStatus.PENDING)
    first = asyncio.run(gateway.execute(refund))
    second = asyncio.run(gateway.execute(refund))
    assert first is GatewayOutcome.PROCESSED
    assert second is GatewayOutcome.PROCESSED
    assert gateway.execute_calls == 2  # called twice, executed once
    assert len(gateway._ledger) == 1


# ---------------------------------------------------------------------
# service flow control over a stub store
# ---------------------------------------------------------------------


class _StubRecoveryStore:
    """Test double for the PgRefundStore recovery surface."""

    def __init__(self, refunds: dict[str, Refund]):
        self._refunds = refunds
        self.claim_result = "claimed"
        self.settle_result = "settled"
        self.kept_result = "recorded"
        self.settled: list[tuple[str, RefundStatus]] = []
        self.kept: list[tuple[str, str]] = []

    def get(self, refund_id: str) -> Refund | None:
        return self._refunds.get(refund_id)

    def find_recoverable(self, now, *, pending_grace_seconds: float):
        return tuple(self._refunds.values())

    def claim_recovery(self, refund_id, *, owner, expires_at, now):
        return self.claim_result

    def settle_recovered(
        self, refund_id, *, expected_owner, status, now, gateway_result
    ):
        self.settled.append((refund_id, status))
        return self.settle_result

    def mark_recovery_kept_unknown(self, refund_id, *, expected_owner, reason, now):
        self.kept.append((refund_id, reason))
        return self.kept_result


class _QueryRaiser:
    def __init__(self, exc: Exception):
        self._exc = exc

    async def query_status(self, refund_id: str) -> GatewayQueryResult:
        raise self._exc


def _run(service, *args):
    import asyncio

    return asyncio.run(service.recover_once())


def test_service_refuses_construction_without_query_capability():
    with pytest.raises(TypeError, match="status-query capability"):
        RecoveryService(_StubRecoveryStore({}), object())  # type: ignore[arg-type]


def test_query_exception_keeps_unknown_and_records_error():
    """A query failure (timeout/transport) says nothing about the refund:
    the outcome stays unknown, the reason is recorded, no settle is
    attempted."""
    stub = _StubRecoveryStore({"REF-1": _refund("REF-1", RefundStatus.UNKNOWN)})
    service = RecoveryService(stub, _QueryRaiser(GatewayQueryError("timeout")))
    summary = _run(service)
    assert summary.kept_unknown == 1
    assert stub.settled == []
    assert stub.kept == [("REF-1", "gateway status query failed: timeout")]


def test_active_claim_reports_claim_unavailable():
    stub = _StubRecoveryStore({"REF-1": _refund("REF-1", RefundStatus.UNKNOWN)})
    stub.claim_result = "active_claim"
    service = RecoveryService(stub, FakeGateway())
    summary = _run(service)
    assert summary.claim_unavailable == 1
    assert stub.settled == []  # nothing was written


def test_lost_ownership_is_reported_and_writes_nothing():
    stub = _StubRecoveryStore({"REF-1": _refund("REF-1", RefundStatus.UNKNOWN)})
    stub.settle_result = "lost_ownership"
    service = RecoveryService(
        stub, FakeGateway(ledger={"REF-1": GatewayOutcome.PROCESSED})
    )
    summary = _run(service)
    assert summary.lost_ownership == 1
    assert summary.settled_processed == 0


def test_already_terminal_is_reported_not_overwritten():
    stub = _StubRecoveryStore({"REF-1": _refund("REF-1", RefundStatus.UNKNOWN)})
    stub.settle_result = "already_terminal"
    service = RecoveryService(
        stub, FakeGateway(ledger={"REF-1": GatewayOutcome.PROCESSED})
    )
    summary = _run(service)
    assert summary.already_terminal == 1
    assert summary.settled_processed == 0


def test_recover_refund_unknown_id_reports_claim_unavailable():
    stub = _StubRecoveryStore({})
    service = RecoveryService(stub, FakeGateway())
    import asyncio

    result = asyncio.run(service.recover_refund("REF-NOPE"))
    assert result.outcome is RecoveryOutcome.CLAIM_UNAVAILABLE
    assert result.error == "not found"


def test_summary_counts_each_outcome():
    stub = _StubRecoveryStore(
        {
            "REF-1": _refund("REF-1", RefundStatus.UNKNOWN),
            "REF-2": _refund("REF-2", RefundStatus.PENDING),
        }
    )
    service = RecoveryService(
        stub,
        FakeGateway(
            ledger={
                "REF-1": GatewayOutcome.PROCESSED,
                "REF-2": GatewayOutcome.REJECTED,
            }
        ),
    )
    summary = _run(service)
    assert summary.discovered == 2
    assert summary.settled_processed == 1
    assert summary.settled_failed == 1
    assert summary.kept_unknown == 0
    assert summary.claimed == 2
    assert [(rid, status.value) for rid, status in stub.settled] == [
        ("REF-1", "processed"),
        ("REF-2", "failed"),
    ]
