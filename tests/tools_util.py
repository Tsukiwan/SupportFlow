"""Helpers for building an isolated ToolSet with injected gateway behavior."""

from datetime import timedelta
from decimal import Decimal

from app.domain.enums import ApprovalStatus, RiskLevel
from app.domain.models import Approval
from app.persistence.memory_stores import (
    ApprovalStore,
    AuditStore,
    OrderStore,
    RefundStore,
    ShippingStore,
    TicketNoteStore,
    TicketStore,
)
from app.persistence.mock_catalog import (
    REFERENCE_TIME,
    build_orders_by_id,
    build_shipments_by_order_id,
    build_tickets_by_id,
)
from app.services.audit import AuditRecorder
from app.services.payments import PaymentGateway, SimulatedPaymentGateway
from app.tools.context import ToolContext
from app.tools.registry import ToolSet


def build_tool_set(
    gateway: PaymentGateway | None = None,
    gateway_outcomes=None,
    approvals: tuple[Approval, ...] = (),
    orders=None,
    tickets=None,
    shipments=None,
) -> ToolSet:
    tool_set = ToolSet(
        orders=OrderStore(orders if orders is not None else build_orders_by_id()),
        refunds=RefundStore(),
        shipments=ShippingStore(
            shipments if shipments is not None else build_shipments_by_order_id()
        ),
        tickets=TicketStore(tickets if tickets is not None else build_tickets_by_id()),
        notes=TicketNoteStore(),
        approvals=ApprovalStore(),
        gateway=gateway or SimulatedPaymentGateway(outcomes=gateway_outcomes),
        recorder=AuditRecorder(AuditStore()),
    )
    for approval in approvals:
        tool_set.approvals.add(approval)
    return tool_set


def make_approval(
    *,
    order_id: str = "ORD-1005",
    ticket_id: str = "TKT-2005",
    amount: Decimal = Decimal("129.90"),
    plan_id: str = "PLAN-1",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    risk_level: RiskLevel = RiskLevel.HIGH,
    expires_at=None,
) -> Approval:
    """An approval for the delivered_not_received scenario (HIGH risk)."""
    decided = status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
    return Approval(
        approval_id="APR-9001",
        ticket_id=ticket_id,
        order_id=order_id,
        plan_id=plan_id,
        action="refund",
        amount=amount,
        risk_level=risk_level,
        justification="Human-reviewed refund request for a disputed delivery.",
        status=status,
        requested_at=REFERENCE_TIME + timedelta(days=8),
        decided_at=REFERENCE_TIME + timedelta(days=9) if decided else None,
        decided_by="alice" if decided else None,
        expires_at=expires_at,
    )


def ctx(ticket_id: str = "TKT-2001", run_id: str | None = None) -> ToolContext:
    return ToolContext(ticket_id=ticket_id, run_id=run_id)
