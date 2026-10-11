"""Day 4 batch 1: FAILED-refund reopen re-checks the same-order gate.

Before this fix, a same-key retry of a FAILED refund skipped the
same-order unresolved scan (the FAILED branch of ``RefundStore.create``
returns before it), so the refund could reopen while another refund of
the same order was already PENDING or UNKNOWN.

The tests below construct that interleaving deterministically with a
barrier-controlled gateway — not by repeating concurrent runs — and
assert persisted state: refund records, reservations and gateway call
counts.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

from app.agent.llm import (
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring
from app.domain.enums import (
    IssueType,
    OrderStatus,
    RefundStatus,
    RiskLevel,
    TicketStatus,
)
from app.domain.models import Order, Ticket
from app.persistence.mock_catalog import REFERENCE_TIME
from app.services.payments import GatewayOutcome, SimulatedPaymentGateway
from app.tools.context import ToolContext
from app.tools.result import ToolErrorCode

T0 = REFERENCE_TIME + timedelta(days=30)


def run(coro):
    return asyncio.run(coro)


def make_ticket(ticket_id: str) -> Ticket:
    return Ticket(
        ticket_id=ticket_id,
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=TicketStatus.OPEN,
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=RiskLevel.LOW,
        subject=f"Gate probe ticket {ticket_id}",
        description="Deterministic regression ticket for the reopen order gate.",
        claimed_amount=Decimal("500.00"),
        created_at=T0 - timedelta(days=1),
        updated_at=T0 - timedelta(days=1),
    )


def make_order() -> Order:
    return Order(
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("500.00"),  # enough balance to isolate the gate
        created_at=T0 - timedelta(days=30),
        delivered_at=T0 - timedelta(days=20),
    )


def make_deps(ticket_id: str, gateway):
    return build_agent_wiring(
        orders={"ORD-1001": make_order()},
        tickets={ticket_id: make_ticket(ticket_id)},
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
        gateway=gateway,
        clock=lambda: T0,
    )


def create(deps, ticket_id, amount, key, plan_id):
    return run(
        deps.tools.create_refund(
            ToolContext(ticket_id=ticket_id, run_id=f"RUN-{key}"),
            order_id="ORD-1001",
            amount=Decimal(amount),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key=key,
            plan_id=plan_id,
        )
    )


class GateControlledGateway(SimulatedPaymentGateway):
    """Parks one ``execute`` call until the test releases it, holding a
    refund deterministically in PENDING while another operation runs."""

    def __init__(self, outcomes):
        super().__init__(outcomes=outcomes)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.park_next = False

    async def execute(self, refund):
        self.execute_calls += 1
        if self.park_next:
            self.park_next = False
            self.entered.set()
            await self.release.wait()
        await asyncio.sleep(0)
        return self._next()


def test_reopen_is_blocked_while_another_refund_of_the_order_is_pending():
    """A FAILED, then B enters PENDING and parks inside the gateway call.
    Retrying A with its original key must be refused before any gateway
    call; A stays FAILED, B stays PENDING and only B holds a reservation."""
    gateway = GateControlledGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED]
    )
    deps = make_deps("TKT-D401", gateway)

    first = create(deps, "TKT-D401", "50.00", "day4-gate-a", "PLAN-A")
    assert first.error_code.value == "UPSTREAM_TIMEOUT"
    assert deps.tools.refund_store.all()[0].status is RefundStatus.FAILED

    async def both():
        gateway.park_next = True
        task_b = asyncio.ensure_future(
            deps.tools.create_refund(
                ToolContext(ticket_id="TKT-D401", run_id="RUN-B"),
                order_id="ORD-1001",
                amount=Decimal("60.00"),
                issue_type=IssueType.REFUND_REQUEST,
                idempotency_key="day4-gate-b",
                plan_id="PLAN-B",
            )
        )
        await gateway.entered.wait()  # B's record is PENDING, in the gateway
        retry = await deps.tools.create_refund(
            ToolContext(ticket_id="TKT-D401", run_id="RUN-A2"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="day4-gate-a",
            plan_id="PLAN-A",
        )
        gateway.release.set()
        result_b = await task_b
        return retry, result_b

    retry, result_b = run(both())

    assert retry.error_code is ToolErrorCode.CONCURRENT_PROCESSING
    assert retry.retryable is True
    # The refused retry never reached the gateway: A's failed execute and
    # B's in-flight execute are the only calls.
    assert gateway.execute_calls == 2
    assert result_b.success
    records = {r.idempotency_key: r for r in deps.tools.refund_store.all()}
    assert records["day4-gate-a"].status is RefundStatus.FAILED
    assert records["day4-gate-b"].status is RefundStatus.PROCESSED
    assert len(records) == 2  # no third record was created
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("60.00")
    assert order.reserved_amount == Decimal("0")  # only B reserved, then settled


def test_reopen_succeeds_when_no_other_refund_is_unresolved():
    """Control: the same FAILED record, retried with its original key while
    nothing else is in flight, reopens and processes normally."""
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED]
    )
    deps = make_deps("TKT-D402", gateway)

    first = create(deps, "TKT-D402", "50.00", "day4-ok-a", "PLAN-A")
    assert first.error_code.value == "UPSTREAM_TIMEOUT"

    retry = create(deps, "TKT-D402", "50.00", "day4-ok-a", "PLAN-A")
    assert retry.success
    records = deps.tools.refund_store.all()
    assert len(records) == 1  # the same record reopened, not replaced
    assert records[0].status is RefundStatus.PROCESSED
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("50.00")
    assert order.reserved_amount == Decimal("0")
    assert gateway.execute_calls == 2


def test_reopen_is_blocked_by_an_unresolved_refund_of_the_order():
    """A FAILED, then B ends UNKNOWN on the same order. Retrying A must be
    refused with UNRESOLVED_REFUND pointing at B; B keeps its reservation
    and no extra gateway call happens."""
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.UNKNOWN]
    )
    deps = make_deps("TKT-D403", gateway)

    first = create(deps, "TKT-D403", "50.00", "day4-unk-a", "PLAN-A")
    assert first.error_code.value == "UPSTREAM_TIMEOUT"
    second = create(deps, "TKT-D403", "60.00", "day4-unk-b", "PLAN-B")
    assert second.error_code.value == "UNKNOWN_OUTCOME"

    retry = create(deps, "TKT-D403", "50.00", "day4-unk-a", "PLAN-A")
    assert retry.error_code is ToolErrorCode.UNRESOLVED_REFUND
    assert retry.data is not None
    assert retry.data.idempotency_key == "day4-unk-b"  # the blocking record
    assert gateway.execute_calls == 2  # the refused retry never executed
    records = {r.idempotency_key: r for r in deps.tools.refund_store.all()}
    assert records["day4-unk-a"].status is RefundStatus.FAILED
    assert records["day4-unk-b"].status is RefundStatus.UNKNOWN
    order = deps.tools.orders.get("ORD-1001")
    assert order.reserved_amount == Decimal("60.00")  # only B's reservation kept
    assert order.refunded_amount == Decimal("0")
