"""Tool set: typed, audited entry points for the agent layer.

Each tool validates its inputs against a pydantic model and returns a
:class:`~app.tools.result.ToolResult` — tools never raise business errors
and never fabricate success. Day 3 will expose these same methods to
LangGraph as the agent's tools.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.domain.enums import IssueType, OrderStatus
from app.domain.models import Order, Refund, Shipment, TicketNote
from app.persistence.memory_stores import (
    ApprovalStore,
    AuditStore,
    OrderStore,
    RefundStore,
    ShippingStore,
    TicketNoteStore,
    TicketStore,
)
from app.services.audit import AuditRecorder
from app.services.payments import PaymentGateway, SimulatedPaymentGateway
from app.services.refund_policy import RefundPolicy, evaluate_refund_policy
from app.services.refunds import RefundService
from app.tools.audit import audited
from app.tools.context import ToolContext
from app.tools.result import ToolErrorCode, ToolResult
from app.tools.schemas import (
    AddTicketNoteInput,
    CreateRefundInput,
    GetOrderInput,
    GetRefundPolicyInput,
    GetShippingStatusInput,
)


class ToolSet:
    """The five Day 2 tools. Stores stay public so tests can assert on
    final persisted state, not just return values."""

    def __init__(
        self,
        *,
        orders: OrderStore,
        refunds: RefundStore,
        shipments: ShippingStore,
        tickets: TicketStore,
        notes: TicketNoteStore,
        approvals: ApprovalStore,
        gateway: PaymentGateway,
        recorder: AuditRecorder,
    ) -> None:
        self.orders = orders
        self.refund_store = refunds
        self.shipments = shipments
        self.tickets = tickets
        self.notes = notes
        self.approvals = approvals
        self.gateway = gateway
        self.audit_store = recorder.store
        self._recorder = recorder
        self._refund_service = RefundService(
            orders=orders,
            refunds=refunds,
            tickets=tickets,
            approvals=approvals,
            gateway=gateway,
            recorder=recorder,
        )

    @audited("get_order")
    async def get_order(
        self, ctx: ToolContext | None = None, *, order_id: str
    ) -> ToolResult[Order]:
        params = GetOrderInput(order_id=order_id)
        order = self.orders.get(params.order_id)
        if order is None:
            return ToolResult.error(
                ToolErrorCode.ORDER_NOT_FOUND,
                f"order {params.order_id} not found",
            )
        return ToolResult.ok("order found", data=order)

    @audited("get_shipping_status")
    async def get_shipping_status(
        self, ctx: ToolContext | None = None, *, order_id: str
    ) -> ToolResult[Shipment]:
        params = GetShippingStatusInput(order_id=order_id)
        if self.orders.get(params.order_id) is None:
            return ToolResult.error(
                ToolErrorCode.ORDER_NOT_FOUND,
                f"order {params.order_id} not found",
            )
        shipment = self.shipments.get(params.order_id)
        if shipment is None:
            return ToolResult.error(
                ToolErrorCode.SHIPPING_NOT_FOUND,
                f"no shipping record for order {params.order_id}",
            )
        return ToolResult.ok("shipping status found", data=shipment)

    @audited("get_refund_policy")
    async def get_refund_policy(
        self,
        ctx: ToolContext | None = None,
        *,
        issue_type: IssueType,
        order_status: OrderStatus,
    ) -> ToolResult[RefundPolicy]:
        params = GetRefundPolicyInput(
            issue_type=issue_type, order_status=order_status
        )
        policy = evaluate_refund_policy(params.issue_type, params.order_status)
        return ToolResult.ok("refund policy evaluated", data=policy)

    @audited("add_ticket_note")
    async def add_ticket_note(
        self, ctx: ToolContext | None = None, *, ticket_id: str, note: str
    ) -> ToolResult[TicketNote]:
        params = AddTicketNoteInput(ticket_id=ticket_id, note=note)
        if self.tickets.get(params.ticket_id) is None:
            return ToolResult.error(
                ToolErrorCode.TICKET_NOT_FOUND,
                f"ticket {params.ticket_id} not found",
            )
        created = TicketNote(
            note_id=f"NOTE-{uuid4().hex[:12]}",
            ticket_id=params.ticket_id,
            author="agent",
            text=params.note,
            created_at=datetime.now(timezone.utc),
        )
        self.notes.add(created)
        return ToolResult.ok("note added", data=created)

    @audited("create_refund")
    async def create_refund(
        self,
        ctx: ToolContext | None = None,
        *,
        order_id: str,
        amount: Decimal,
        issue_type: IssueType,
        idempotency_key: str,
        plan_id: str | None = None,
    ) -> ToolResult[Refund]:
        params = CreateRefundInput(
            order_id=order_id,
            amount=amount,
            issue_type=issue_type,
            idempotency_key=idempotency_key,
            plan_id=plan_id,
        )
        return await self._refund_service.create(params, ctx)


def build_default_tool_set(gateway: PaymentGateway | None = None) -> ToolSet:
    """Assemble a tool set over the deterministic mock catalog."""
    from app.persistence.mock_catalog import (
        build_orders_by_id,
        build_shipments_by_order_id,
        build_tickets_by_id,
    )

    recorder = AuditRecorder(AuditStore())
    return ToolSet(
        orders=OrderStore(build_orders_by_id()),
        refunds=RefundStore(),
        shipments=ShippingStore(build_shipments_by_order_id()),
        tickets=TicketStore(build_tickets_by_id()),
        notes=TicketNoteStore(),
        approvals=ApprovalStore(),
        gateway=gateway or SimulatedPaymentGateway(),
        recorder=recorder,
    )
