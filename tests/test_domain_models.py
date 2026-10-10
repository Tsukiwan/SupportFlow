"""Domain model constraints and cross-field validation rules."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    OrderStatus,
    RiskLevel,
)
from app.domain.models import (
    AgentRun,
    Approval,
    AuditEvent,
    Order,
    Refund,
    ResolutionPlan,
    ResolutionPlanStep,
    Ticket,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def make_ticket(**overrides) -> dict:
    fields = dict(
        ticket_id="TKT-1",
        order_id="ORD-1",
        customer_id="CUS-1",
        issue_type=IssueType.REFUND_REQUEST,
        subject="Refund my order",
        description="A sufficiently long description of the problem.",
        claimed_amount=Decimal("19.99"),
        created_at=T0,
        updated_at=T0,
    )
    fields.update(overrides)
    return fields


# --- Order ---


def test_order_rejects_delivered_before_created():
    with pytest.raises(ValidationError):
        Order(
            order_id="ORD-1",
            customer_id="CUS-1",
            status=OrderStatus.DELIVERED,
            currency="USD",
            total_amount=Decimal("10.00"),
            created_at=T0 + timedelta(days=2),
            delivered_at=T0,
        )


def test_order_rejects_bad_currency():
    with pytest.raises(ValidationError):
        Order(
            order_id="ORD-1",
            customer_id="CUS-1",
            status=OrderStatus.PAID,
            currency="usd",
            total_amount=Decimal("10.00"),
            created_at=T0,
        )


def test_order_refundable_amount():
    order = Order(
        order_id="ORD-1",
        customer_id="CUS-1",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("199.99"),
        refunded_amount=Decimal("30.00"),
        created_at=T0,
    )
    assert order.refundable_amount == Decimal("169.99")


def test_order_is_frozen():
    order = Order(
        order_id="ORD-1",
        customer_id="CUS-1",
        status=OrderStatus.PAID,
        currency="USD",
        total_amount=Decimal("10.00"),
        created_at=T0,
    )
    with pytest.raises((TypeError, ValidationError)):
        order.status = OrderStatus.CANCELLED  # type: ignore[misc]


# --- Ticket ---


def test_ticket_rejects_updated_before_created():
    with pytest.raises(ValidationError):
        Ticket(**make_ticket(updated_at=T0 - timedelta(seconds=1)))


def test_refund_request_requires_claimed_amount():
    with pytest.raises(ValidationError):
        Ticket(**make_ticket(claimed_amount=None))


def test_non_refund_ticket_may_omit_claimed_amount():
    ticket = Ticket(
        **make_ticket(issue_type=IssueType.DELIVERY_ISSUE, claimed_amount=None)
    )
    assert ticket.claimed_amount is None


def test_ticket_serializes_enums_to_plain_strings():
    ticket = Ticket(**make_ticket())
    dumped = ticket.model_dump(mode="json")
    assert dumped["status"] == "open"
    assert dumped["issue_type"] == "refund_request"


def test_ticket_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        Ticket(**make_ticket(created_at=datetime(2026, 9, 1, 10, 0)))


# --- Refund ---


def test_refund_rejects_zero_amount():
    with pytest.raises(ValidationError):
        Refund(
            refund_id="REF-1",
            idempotency_key="idem-test-0001",
            ticket_id="TKT-1",
            order_id="ORD-1",
            amount=Decimal("0"),
            currency="USD",
            issue_type=IssueType.REFUND_REQUEST,
            reason="Refund requested by the customer.",
            created_at=T0,
        )


def test_refund_rejects_processed_before_created():
    with pytest.raises(ValidationError):
        Refund(
            refund_id="REF-1",
            idempotency_key="idem-test-0001",
            ticket_id="TKT-1",
            order_id="ORD-1",
            amount=Decimal("10.00"),
            currency="USD",
            issue_type=IssueType.REFUND_REQUEST,
            reason="Refund requested by the customer.",
            created_at=T0,
            processed_at=T0 - timedelta(seconds=1),
        )


# --- Approval ---


def test_approval_decided_requires_decision_fields():
    with pytest.raises(ValidationError):
        Approval(
            approval_id="APR-1",
            ticket_id="TKT-1",
            order_id="ORD-1",
            plan_id="PLAN-1",
            action="refund",
            amount=Decimal("10.00"),
            risk_level=RiskLevel.HIGH,
            justification="High-value refund requires a human decision.",
            status=ApprovalStatus.APPROVED,
            requested_at=T0,
        )


def test_approval_pending_must_not_carry_decision_fields():
    with pytest.raises(ValidationError):
        Approval(
            approval_id="APR-1",
            ticket_id="TKT-1",
            order_id="ORD-1",
            plan_id="PLAN-1",
            action="refund",
            amount=Decimal("10.00"),
            risk_level=RiskLevel.HIGH,
            justification="High-value refund requires a human decision.",
            status=ApprovalStatus.PENDING,
            requested_at=T0,
            decided_at=T0,
            decided_by="alice",
        )


def test_approval_decision_is_consistent():
    approval = Approval(
        approval_id="APR-1",
        ticket_id="TKT-1",
        order_id="ORD-1",
        plan_id="PLAN-1",
        action="refund",
        amount=Decimal("10.00"),
        risk_level=RiskLevel.HIGH,
        justification="High-value refund requires a human decision.",
        status=ApprovalStatus.APPROVED,
        requested_at=T0,
        decided_at=T0 + timedelta(hours=1),
        decided_by="alice",
    )
    assert approval.status is ApprovalStatus.APPROVED


# --- AgentRun ---


def test_agent_run_caps_iterations():
    with pytest.raises(ValidationError):
        AgentRun(
            run_id="RUN-1",
            ticket_id="TKT-1",
            started_at=T0,
            iterations=11,
            max_iterations=10,
        )


def test_agent_run_terminal_requires_finished_at():
    with pytest.raises(ValidationError):
        AgentRun(run_id="RUN-1", ticket_id="TKT-1", started_at=T0, status=AgentRunStatus.COMPLETED)


def test_agent_run_error_only_when_failed():
    with pytest.raises(ValidationError):
        AgentRun(
            run_id="RUN-1",
            ticket_id="TKT-1",
            started_at=T0,
            finished_at=T0,
            status=AgentRunStatus.COMPLETED,
            error="should not be here",
        )


def test_agent_run_failed_with_error_is_valid():
    run = AgentRun(
        run_id="RUN-1",
        ticket_id="TKT-1",
        started_at=T0,
        finished_at=T0 + timedelta(minutes=2),
        status=AgentRunStatus.FAILED,
        iterations=3,
        error="iteration limit exceeded",
    )
    assert run.error == "iteration limit exceeded"


# --- AuditEvent ---


def test_audit_event_rejects_secret_keys():
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="EVT-1",
            timestamp=T0,
            actor="agent",
            action="lookup_order",
            details={"deepseek_api_key": "sk-deepseek-..."},
        )


def test_audit_event_accepts_normal_details():
    event = AuditEvent(
        event_id="EVT-1",
        timestamp=T0,
        actor="agent",
        action="lookup_order",
        order_id="ORD-1",
        details={"order_total": "199.99"},
    )
    assert event.details == {"order_total": "199.99"}


# --- ResolutionPlan ---


def test_plan_high_risk_requires_approval():
    with pytest.raises(ValidationError):
        ResolutionPlan(
            plan_id="PLAN-1",
            ticket_id="TKT-1",
            risk_level=RiskLevel.HIGH,
            requires_approval=False,
            steps=(ResolutionPlanStep(action="issue refund", reason="within policy"),),
            created_at=T0,
        )


def test_plan_high_risk_with_approval_is_valid():
    plan = ResolutionPlan(
        plan_id="PLAN-1",
        ticket_id="TKT-1",
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
        steps=(ResolutionPlanStep(action="issue refund", reason="within policy"),),
        created_at=T0,
    )
    assert plan.requires_approval is True


def test_plan_low_risk_may_skip_approval():
    plan = ResolutionPlan(
        plan_id="PLAN-1",
        ticket_id="TKT-1",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        steps=(ResolutionPlanStep(action="explain policy", reason="customer asked"),),
        created_at=T0,
    )
    assert plan.requires_approval is False


def test_plan_requires_at_least_one_step():
    with pytest.raises(ValidationError):
        ResolutionPlan(
            plan_id="PLAN-1",
            ticket_id="TKT-1",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            steps=(),
            created_at=T0,
        )
