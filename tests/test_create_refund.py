"""create_refund: safety checks, idempotency, concurrency, approvals.

Every test asserts on final persisted state (refund records + live order
balance), not just return values.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import model_validator

from app.domain.enums import ApprovalStatus, IssueType, RefundStatus, RiskLevel
from app.persistence import memory_stores
from app.persistence import mock_catalog
from app.persistence.mock_catalog import build_tickets_by_id
from app.services.payments import GatewayOutcome
from app.tools.result import ToolErrorCode
from tests.tools_util import build_tool_set, ctx, make_approval


def run(coro):
    return asyncio.run(coro)


def refund_args(order_id="ORD-1001", amount="199.99", issue_type=IssueType.REFUND_REQUEST,
                idempotency_key="idem-default-01", plan_id=None, ticket_id="TKT-2001"):
    return dict(
        ctx=ctx(ticket_id=ticket_id),
        order_id=order_id,
        amount=Decimal(amount),
        issue_type=issue_type,
        idempotency_key=idempotency_key,
        plan_id=plan_id,
    )


# --- happy path and final state ---


def test_happy_path_creates_processed_refund():
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args()))
    assert result.success is True
    refund = result.data
    assert refund.status is RefundStatus.PROCESSED
    assert refund.order_id == "ORD-1001"
    assert refund.amount == Decimal("199.99")
    assert refund.idempotency_key == "idem-default-01"
    assert refund.processed_at is not None
    # final persisted state
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("199.99")


def test_refund_requires_ticket_context():
    ts = build_tool_set()
    result = run(ts.create_refund(ctx=None, order_id="ORD-1001", amount=Decimal("10.00"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-no-ctx-01"))
    assert result.error_code is ToolErrorCode.INVALID_INPUT
    assert ts.refund_store.count() == 0


# --- order existence and policy ---


def test_refund_unknown_order():
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(order_id="ORD-9999")))
    assert result.error_code is ToolErrorCode.ORDER_NOT_FOUND
    assert ts.refund_store.count() == 0


def test_issue_type_mislabel_is_rejected():
    """The caller cannot claim a different issue type than the ticket
    record — the policy is evaluated from the ticket, not from arguments."""
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(
        issue_type=IssueType.ORDER_INQUIRY, idempotency_key="idem-policy-001")))
    assert result.error_code is ToolErrorCode.ISSUE_TYPE_MISMATCH
    assert ts.refund_store.count() == 0
    order = ts.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("0")
    assert order.reserved_amount == Decimal("0")
    assert ts.gateway.execute_calls == 0


def test_high_risk_ticket_cannot_relabel_to_skip_approval():
    """The approval-gate bypass: a HIGH-risk DELIVERY_ISSUE ticket must not
    pass as an approval-free REFUND_REQUEST."""
    ts = build_tool_set()
    result = run(ts.create_refund(ctx=ctx(ticket_id="TKT-2005"),
                                  order_id="ORD-1005", amount=Decimal("129.90"),
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-relabel-01"))
    assert result.error_code is ToolErrorCode.ISSUE_TYPE_MISMATCH
    assert ts.refund_store.count() == 0
    order = ts.orders.get("ORD-1005")
    assert order.refunded_amount == Decimal("0")
    assert order.reserved_amount == Decimal("0")
    assert ts.gateway.execute_calls == 0


def test_policy_denied_with_trusted_ticket_issue_type():
    """Genuine policy denials (issue type taken from the ticket record)
    still leave no records."""
    tickets = build_tickets_by_id()
    tickets["TKT-2007"] = mock_catalog.ticket_non_existent().model_copy(
        update={"ticket_id": "TKT-2007", "order_id": "ORD-1001"}
    )
    ts = build_tool_set(tickets=tickets)
    result = run(ts.create_refund(**refund_args(
        issue_type=IssueType.ORDER_INQUIRY, idempotency_key="idem-policy-001",
        ticket_id="TKT-2007")))
    assert result.error_code is ToolErrorCode.POLICY_DENIED
    assert ts.refund_store.count() == 0
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")


def test_delivery_issue_on_cancelled_order_is_denied():
    tickets = build_tickets_by_id()
    tickets["TKT-2003"] = mock_catalog.ticket_cancelled().model_copy(
        update={"issue_type": IssueType.DELIVERY_ISSUE}
    )
    ts = build_tool_set(tickets=tickets)
    result = run(ts.create_refund(**refund_args(
        order_id="ORD-1003", amount="45.00", issue_type=IssueType.DELIVERY_ISSUE,
        idempotency_key="idem-cancelled-01", ticket_id="TKT-2003")))
    assert result.error_code is ToolErrorCode.POLICY_DENIED
    assert ts.refund_store.count() == 0


# --- ticket binding (ticket <-> order) ---


def test_refund_unknown_ticket():
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(ticket_id="TKT-9999")))
    assert result.error_code is ToolErrorCode.TICKET_NOT_FOUND
    assert ts.refund_store.count() == 0
    assert ts.gateway.execute_calls == 0


def test_ticket_order_mismatch_is_rejected():
    """TKT-2006 is bound to the missing ORD-9999; refunding an existing
    but unrelated order under it must be rejected."""
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(ticket_id="TKT-2006")))
    assert result.error_code is ToolErrorCode.TICKET_ORDER_MISMATCH
    assert ts.refund_store.count() == 0
    order = ts.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("0")
    assert order.reserved_amount == Decimal("0")
    assert ts.gateway.execute_calls == 0


# --- amount validation ---


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-5"), Decimal("10.999")])
def test_invalid_amounts_are_rejected(amount):
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(amount=amount,
                                                idempotency_key="idem-invalid-01")))
    assert result.error_code is ToolErrorCode.INVALID_INPUT
    assert ts.refund_store.count() == 0


def test_float_amount_is_rejected():
    ts = build_tool_set()
    result = run(ts.create_refund(ctx=ctx(), order_id="ORD-1001", amount=19.5,
                                  issue_type=IssueType.REFUND_REQUEST,
                                  idempotency_key="idem-float-0001"))
    assert result.error_code is ToolErrorCode.INVALID_INPUT
    assert ts.refund_store.count() == 0


# --- live balance ---


def test_amount_exceeds_balance():
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(amount="200.00",
                                                idempotency_key="idem-over-0001")))
    assert result.error_code is ToolErrorCode.AMOUNT_MISMATCH
    assert ts.refund_store.count() == 0
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")


def test_already_refunded_order():
    ts = build_tool_set()
    result = run(ts.create_refund(**refund_args(
        order_id="ORD-1002", amount="89.50", idempotency_key="idem-refunded-01",
        ticket_id="TKT-2002")))
    assert result.error_code is ToolErrorCode.ALREADY_REFUNDED
    assert ts.refund_store.count() == 0


def test_partial_refunds_track_live_balance():
    ts = build_tool_set()
    r1 = run(ts.create_refund(**refund_args(amount="50.00", idempotency_key="idem-part-001")))
    r2 = run(ts.create_refund(**refund_args(amount="50.00", idempotency_key="idem-part-002")))
    assert r1.success and r2.success
    r3 = run(ts.create_refund(**refund_args(amount="100.00", idempotency_key="idem-part-003")))
    assert r3.error_code is ToolErrorCode.AMOUNT_MISMATCH  # remaining balance is 99.99
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("100.00")
    assert ts.refund_store.count() == 2


# --- idempotency ---


def test_same_key_same_payload_is_idempotent():
    ts = build_tool_set()
    args = refund_args(amount="50.00", idempotency_key="idem-repeat-001")
    r1 = run(ts.create_refund(**args))
    r2 = run(ts.create_refund(**args))
    assert r1.success and r2.success
    assert r1.data.refund_id == r2.data.refund_id
    assert ts.refund_store.count() == 1  # no second refund record
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("50.00")  # no double charge


def test_same_key_different_order_conflicts():
    ts = build_tool_set()
    run(ts.create_refund(**refund_args(amount="50.00", idempotency_key="idem-conflict-01")))
    result = run(ts.create_refund(**refund_args(
        order_id="ORD-1002", amount="50.00", idempotency_key="idem-conflict-01",
        ticket_id="TKT-2002")))
    assert result.error_code is ToolErrorCode.IDEMPOTENCY_CONFLICT
    assert ts.refund_store.count() == 1


def test_same_key_different_amount_conflicts():
    ts = build_tool_set()
    run(ts.create_refund(**refund_args(amount="50.00", idempotency_key="idem-conflict-02")))
    result = run(ts.create_refund(**refund_args(amount="60.00", idempotency_key="idem-conflict-02")))
    assert result.error_code is ToolErrorCode.IDEMPOTENCY_CONFLICT
    assert ts.refund_store.count() == 1


def test_same_key_different_ticket_conflicts():
    tickets = build_tickets_by_id()
    tickets["TKT-2007"] = mock_catalog.ticket_refundable().model_copy(
        update={"ticket_id": "TKT-2007"}
    )
    ts = build_tool_set(tickets=tickets)
    run(ts.create_refund(**refund_args(amount="50.00",
                                       idempotency_key="idem-intent-tkt-01")))
    result = run(ts.create_refund(**refund_args(
        amount="50.00", idempotency_key="idem-intent-tkt-01",
        ticket_id="TKT-2007")))
    assert result.error_code is ToolErrorCode.IDEMPOTENCY_CONFLICT
    assert ts.refund_store.count() == 1


def test_same_key_different_plan_id_conflicts():
    ts = build_tool_set()
    run(ts.create_refund(**refund_args(amount="50.00",
                                       idempotency_key="idem-intent-plan-01")))
    result = run(ts.create_refund(**refund_args(
        amount="50.00", idempotency_key="idem-intent-plan-01",
        plan_id="PLAN-9")))
    assert result.error_code is ToolErrorCode.IDEMPOTENCY_CONFLICT
    assert ts.refund_store.count() == 1


def test_same_key_different_issue_type_is_rejected():
    """A different issue type with the same key is rejected explicitly
    (before the idempotency lookup); it must never create a second record
    or silently replay the first."""
    ts = build_tool_set()
    run(ts.create_refund(**refund_args(amount="50.00",
                                       idempotency_key="idem-intent-issue-01")))
    result = run(ts.create_refund(**refund_args(
        amount="50.00", idempotency_key="idem-intent-issue-01",
        issue_type=IssueType.ORDER_INQUIRY)))
    assert result.error_code is ToolErrorCode.ISSUE_TYPE_MISMATCH
    assert ts.refund_store.count() == 1


# --- concurrency ---


def test_concurrent_refunds_same_order_single_winner():
    ts = build_tool_set()

    async def attempt(key):
        return await ts.create_refund(**refund_args(idempotency_key=key))

    async def both():
        return await asyncio.gather(attempt("idem-race-0001"), attempt("idem-race-0002"))

    r1, r2 = asyncio.run(both())
    assert sorted([r.success for r in (r1, r2)]) == [False, True]
    failure = r1 if not r1.success else r2
    assert failure.error_code in (
        ToolErrorCode.CONCURRENT_PROCESSING, ToolErrorCode.AMOUNT_MISMATCH)
    # final state: exactly one processed refund, balance reserved once
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("199.99")


def test_concurrent_same_idempotency_key():
    ts = build_tool_set()

    async def attempt():
        return await ts.create_refund(**refund_args(idempotency_key="idem-same-key-01"))

    async def both():
        return await asyncio.gather(attempt(), attempt())

    r1, r2 = asyncio.run(both())
    assert sorted([r.success for r in (r1, r2)]) == [False, True]
    assert ts.refund_store.count() == 1
    refund_id = ts.refund_store.all()[0].refund_id
    # a later call with the same key resolves to the same record
    r3 = run(ts.create_refund(**refund_args(idempotency_key="idem-same-key-01")))
    assert r3.success is True
    assert r3.data.refund_id == refund_id
    assert ts.refund_store.count() == 1


# --- approval gate (delivered_not_received, HIGH risk) ---


def dni_args(**overrides):
    args = refund_args(
        order_id="ORD-1005", amount="129.90", issue_type=IssueType.DELIVERY_ISSUE,
        idempotency_key="idem-approval-01", ticket_id="TKT-2005", plan_id="PLAN-1",
    )
    args.update(overrides)
    return args


def test_refund_requiring_approval_without_one_is_blocked():
    ts = build_tool_set()
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_REQUIRED
    assert ts.refund_store.count() == 0


def test_pending_approval_blocks_refund():
    ts = build_tool_set(approvals=(make_approval(status=ApprovalStatus.PENDING),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_REQUIRED
    assert ts.refund_store.count() == 0


def test_rejected_approval_blocks_refund():
    ts = build_tool_set(approvals=(make_approval(status=ApprovalStatus.REJECTED),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_REJECTED
    assert ts.refund_store.count() == 0


def test_expired_approval_blocks_refund():
    ts = build_tool_set(approvals=(make_approval(status=ApprovalStatus.EXPIRED),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_EXPIRED
    assert ts.refund_store.count() == 0


def test_approval_amount_mismatch():
    ts = build_tool_set(approvals=(make_approval(amount=Decimal("100.00")),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert ts.refund_store.count() == 0


def test_approval_order_mismatch():
    ts = build_tool_set(approvals=(make_approval(order_id="ORD-1001"),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert ts.refund_store.count() == 0


def test_approval_plan_version_mismatch():
    ts = build_tool_set(approvals=(make_approval(plan_id="PLAN-9"),))
    result = run(ts.create_refund(**dni_args(plan_id="PLAN-1")))
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert ts.refund_store.count() == 0


def test_approval_missing_plan_id_in_request():
    ts = build_tool_set(approvals=(make_approval(),))
    result = run(ts.create_refund(**dni_args(plan_id=None)))
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert ts.refund_store.count() == 0


def test_matching_approval_allows_refund():
    ts = build_tool_set(approvals=(make_approval(),))
    result = run(ts.create_refund(**dni_args()))
    assert result.success is True
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1005").refunded_amount == Decimal("129.90")
    approval_events = [
        e for e in ts.audit_store.all()
        if e.action == "create_refund.approval_verified"
    ]
    assert len(approval_events) == 1
    assert approval_events[0].details.get("approval_id") == "APR-9001"


def test_approval_risk_level_below_policy_is_rejected():
    """An approval granted for a lower risk than the policy requires must
    not unlock the refund."""
    ts = build_tool_set(approvals=(make_approval(risk_level=RiskLevel.LOW),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert ts.refund_store.count() == 0


def test_approval_past_validity_window_is_rejected():
    ts = build_tool_set(approvals=(make_approval(
        expires_at=datetime.now(timezone.utc) - timedelta(days=1)),))
    result = run(ts.create_refund(**dni_args()))
    assert result.error_code is ToolErrorCode.APPROVAL_EXPIRED
    assert ts.refund_store.count() == 0


def test_approval_within_validity_window_allows_refund():
    ts = build_tool_set(approvals=(make_approval(
        expires_at=datetime.now(timezone.utc) + timedelta(days=1)),))
    result = run(ts.create_refund(**dni_args()))
    assert result.success is True
    assert ts.refund_store.count() == 1


# --- gateway outcomes ---


def test_transient_failure_releases_balance_and_retry_succeeds():
    ts = build_tool_set(gateway_outcomes=[
        GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED])
    args = refund_args(amount="50.00", idempotency_key="idem-transient-01")
    first = run(ts.create_refund(**args))
    assert first.success is False
    assert first.error_code is ToolErrorCode.UPSTREAM_TIMEOUT
    assert first.retryable is True
    # transient failure: record FAILED, reservation released
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.FAILED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    second = run(ts.create_refund(**args))
    assert second.success is True
    assert ts.refund_store.count() == 1  # still one record, now processed
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("50.00")


def test_unknown_outcome_reserves_balance_and_resolves_by_querying():
    ts = build_tool_set(gateway_outcomes=[
        GatewayOutcome.UNKNOWN, GatewayOutcome.PROCESSED])
    args = refund_args(amount="50.00", idempotency_key="idem-unknown-01")
    first = run(ts.create_refund(**args))
    assert first.success is False
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert first.retryable is False
    record = ts.refund_store.all()[0]
    assert record.status is RefundStatus.UNKNOWN
    # fail closed: the reservation is kept while the outcome is unknown,
    # and it is NOT reported as an already refunded amount
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    second = run(ts.create_refund(**args))
    assert second.success is True
    assert second.data.refund_id == record.refund_id
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("0")


def test_unknown_outcome_stays_unknown_without_resolution():
    ts = build_tool_set(gateway_outcomes=[
        GatewayOutcome.UNKNOWN, GatewayOutcome.UNKNOWN])
    args = refund_args(amount="50.00", idempotency_key="idem-unknown-02")
    first = run(ts.create_refund(**args))
    second = run(ts.create_refund(**args))
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert second.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert ts.refund_store.count() == 1  # never a second refund record
    assert ts.refund_store.all()[0].status is RefundStatus.UNKNOWN
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")


def test_gateway_rejection_releases_balance():
    ts = build_tool_set(gateway_outcomes=[GatewayOutcome.REJECTED])
    result = run(ts.create_refund(**refund_args(amount="50.00",
                                                idempotency_key="idem-reject-01")))
    assert result.error_code is ToolErrorCode.GATEWAY_REJECTED
    assert ts.refund_store.all()[0].status is RefundStatus.REJECTED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("0")


class _BoomGateway:
    async def execute(self, refund):
        raise RuntimeError("gateway exploded")

    async def status(self, refund_id):
        raise RuntimeError("gateway exploded")


class _RaiseThenResolveGateway:
    """execute raises (outcome unknown), status reports the original
    operation's real result."""

    def __init__(self, status_outcome: GatewayOutcome = GatewayOutcome.PROCESSED):
        self.execute_calls = 0
        self.status_calls = 0
        self.status_outcome = status_outcome

    async def execute(self, refund):
        self.execute_calls += 1
        raise ConnectionError("connection lost after sending the request")

    async def status(self, refund_id):
        self.status_calls += 1
        return self.status_outcome


def test_gateway_exception_is_treated_as_unknown_not_failed():
    """An exception carries no proof the gateway did NOT execute the
    refund — it must not release the reservation."""
    ts = build_tool_set(gateway=_BoomGateway())
    result = run(ts.create_refund(**refund_args(amount="50.00",
                                                idempotency_key="idem-boom-0001")))
    assert result.success is False
    assert result.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert result.retryable is False
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.UNKNOWN
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")


def test_gateway_exception_resolves_via_status_query_without_second_execute():
    gateway = _RaiseThenResolveGateway()
    ts = build_tool_set(gateway=gateway)
    args = refund_args(amount="50.00", idempotency_key="idem-raise-0001")
    first = run(ts.create_refund(**args))
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    record = ts.refund_store.all()[0]
    assert record.status is RefundStatus.UNKNOWN
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    # same-key retry must query the original operation, never re-execute
    second = run(ts.create_refund(**args))
    assert second.success is True
    assert second.data.refund_id == record.refund_id
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("0")
    assert gateway.execute_calls == 1  # never a second execution
    assert gateway.status_calls == 1   # resolved by querying the original op


def test_gateway_exception_and_status_failure_stays_unknown():
    """When the exception happened AND the status query fails, the record
    stays UNKNOWN with the reservation held — never reported as failed."""
    ts = build_tool_set(gateway=_BoomGateway())
    args = refund_args(amount="50.00", idempotency_key="idem-raise-0002")
    first = run(ts.create_refund(**args))
    second = run(ts.create_refund(**args))
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert second.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.UNKNOWN
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")


def test_failures_never_produce_spurious_success_records():
    """After business rejections and transient failures there is no
    PROCESSED refund anywhere."""
    ts = build_tool_set(gateway_outcomes=[GatewayOutcome.TRANSIENT_FAILURE])
    run(ts.create_refund(**refund_args(amount="50.00", idempotency_key="idem-spur-001")))
    run(ts.create_refund(**refund_args(issue_type=IssueType.ORDER_INQUIRY,
                                       idempotency_key="idem-spur-002")))
    assert all(r.status is not RefundStatus.PROCESSED for r in ts.refund_store.all())
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("0")


# --- cancellation and timeout recovery ---


class _BlockingGateway:
    """execute blocks until the task is cancelled (the first call only,
    so later refunds behave like PROCESSED); status reports the original
    operation's result."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.execute_calls = 0
        self.status_calls = 0
        self._blocks = True

    async def execute(self, refund):
        self.execute_calls += 1
        if self._blocks:
            self._blocks = False
            self.entered.set()
            await asyncio.Event().wait()
        return GatewayOutcome.PROCESSED

    async def status(self, refund_id):
        self.status_calls += 1
        return GatewayOutcome.PROCESSED


async def cancel_refund_in_flight(ts, **kwargs):
    gateway = ts.gateway
    task = asyncio.create_task(ts.create_refund(**kwargs))
    await asyncio.wait_for(gateway.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancelled_refund_records_unknown_and_recovers_via_status():
    """Cancellation says nothing about whether the gateway executed the
    refund: the record must stay recoverable as UNKNOWN with the
    reservation held, never PENDING-forever and never reported as success."""
    ts = build_tool_set(gateway=_BlockingGateway())
    args = refund_args(amount="50.00", idempotency_key="idem-cancel-0001")
    asyncio.run(cancel_refund_in_flight(ts, **args))
    record = ts.refund_store.all()[0]
    assert record.status is RefundStatus.UNKNOWN
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("0")
    # the audit trail records the cancellation distinctly, never as success
    events = [e for e in ts.audit_store.all() if e.action == "create_refund"]
    assert len(events) == 1
    assert events[0].details["outcome"] == "cancelled"
    assert "error_code" not in events[0].details
    # a same-key retry resolves through the status query without re-executing
    result = run(ts.create_refund(**args))
    assert result.success is True
    assert result.data.refund_id == record.refund_id
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("50.00")
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("0")
    assert ts.gateway.execute_calls == 1  # only the cancelled attempt
    assert ts.gateway.status_calls == 1   # recovery queried, never re-executed


def test_cancelled_refund_does_not_block_other_keys_or_overrefund():
    """An order with an unresolved (UNKNOWN) refund takes no new refunds
    until the original operation is settled — and the gate is lifted by
    resolving it, never by releasing its reservation."""
    ts = build_tool_set(gateway=_BlockingGateway())
    asyncio.run(cancel_refund_in_flight(
        ts, **refund_args(amount="100.00", idempotency_key="idem-cancel-0002")))
    # different key while UNKNOWN: blocked, no new execution, no release
    blocked = run(ts.create_refund(**refund_args(
        amount="50.00", idempotency_key="idem-cancel-other-01")))
    assert blocked.error_code is ToolErrorCode.UNRESOLVED_REFUND
    assert blocked.retryable is False
    assert blocked.data.refund_id == ts.refund_store.all()[0].refund_id
    assert ts.refund_store.count() == 1
    assert ts.gateway.execute_calls == 1  # no new execution attempted
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("100.00")
    # same-key retry resolves the original operation via the status query
    resolved = run(ts.create_refund(**refund_args(
        amount="100.00", idempotency_key="idem-cancel-0002")))
    assert resolved.success is True
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.orders.get("ORD-1001").refunded_amount == Decimal("100.00")
    # only now may a new refund start, still bound by the live balance
    r1 = run(ts.create_refund(**refund_args(
        amount="99.99", idempotency_key="idem-cancel-other-02")))
    assert r1.success is True
    r2 = run(ts.create_refund(**refund_args(
        amount="0.01", idempotency_key="idem-cancel-other-03")))
    # the order is now fully refunded: no over-refund is possible
    assert r2.error_code is ToolErrorCode.ALREADY_REFUNDED
    order = ts.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("199.99")
    assert order.reserved_amount == Decimal("0")
    assert ts.refund_store.count() == 2


def test_unknown_refund_blocks_different_key_refunds():
    """UNKNOWN + different key: gated. Same key: still resolves through
    the status query. Repeated attempts never create a second record."""
    ts = build_tool_set(gateway_outcomes=[
        GatewayOutcome.UNKNOWN, GatewayOutcome.UNKNOWN])
    args = refund_args(amount="50.00", idempotency_key="idem-unknowngate-01")
    first = run(ts.create_refund(**args))
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    blocked = run(ts.create_refund(**refund_args(
        amount="20.00", idempotency_key="idem-unknowngate-02")))
    assert blocked.error_code is ToolErrorCode.UNRESOLVED_REFUND
    assert ts.refund_store.count() == 1
    assert ts.orders.get("ORD-1001").reserved_amount == Decimal("50.00")
    retry = run(ts.create_refund(**args))
    assert retry.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    assert ts.refund_store.count() == 1
    assert ts.refund_store.all()[0].status is RefundStatus.UNKNOWN
    assert ts.gateway.execute_calls == 1
    assert ts.gateway.status_calls == 1


def test_new_refund_allowed_after_unknown_resolves_to_processed():
    ts = build_tool_set(gateway_outcomes=[
        GatewayOutcome.UNKNOWN, GatewayOutcome.PROCESSED])
    args = refund_args(amount="50.00", idempotency_key="idem-resolve-0001")
    first = run(ts.create_refund(**args))
    assert first.error_code is ToolErrorCode.UNKNOWN_OUTCOME
    blocked = run(ts.create_refund(**refund_args(
        amount="20.00", idempotency_key="idem-resolve-0002")))
    assert blocked.error_code is ToolErrorCode.UNRESOLVED_REFUND
    resolved = run(ts.create_refund(**args))
    assert resolved.success is True
    followup = run(ts.create_refund(**refund_args(
        amount="20.00", idempotency_key="idem-resolve-0002")))
    assert followup.success is True
    assert ts.refund_store.count() == 2
    order = ts.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("70.00")
    assert order.reserved_amount == Decimal("0")


# --- construction failure must not leak a reservation ---


def test_refund_construction_failure_leaves_no_reservation(monkeypatch):
    ts = build_tool_set()

    class ExplodingRefund(memory_stores.Refund):
        @model_validator(mode="after")
        def _explode(self):
            raise ValueError("construction failed")

    monkeypatch.setattr(memory_stores, "Refund", ExplodingRefund)
    result = run(ts.create_refund(**refund_args(
        amount="50.00", idempotency_key="idem-f8-0001")))
    assert result.success is False
    assert ts.refund_store.count() == 0
    order = ts.orders.get("ORD-1001")
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("0")
    assert ts.gateway.execute_calls == 0
