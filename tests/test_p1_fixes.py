"""Regression tests for the three P1 findings of the adversarial review.

P1-1: approvals without a plan content hash fail closed — at the graph
      gate and at the RefundService boundary — whenever a plan store is
      wired in; the hash-less plans-less construction survives only as a
      Day 2 test-fixture path.
P1-2: the refund service computes the effective risk and enforces the
      risk-based approval rule independently of the graph, so callers
      that bypass the graph cannot refund a HIGH/CRITICAL ticket without
      an approval.
P1-3: ticket-level claim dedup inside the atomic store section: at most
      one refund per claim across concurrent runs and settled-but-
      unresolved tickets, while the UNKNOWN gate and other tickets on the
      same order are unaffected.

All tests are offline (no API key) and assert persisted state — refund
records, balances, approvals and gateway call counts — not reply text.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agent.llm import DefaultClassifier, DefaultPlanProposer, DefaultReplyGenerator
from app.agent.runner import build_agent_wiring, run_ticket
from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    OrderStatus,
    RefundStatus,
    RiskLevel,
    TicketStatus,
)
from app.domain.models import (
    Approval,
    Order,
    ResolutionPlan,
    ResolutionPlanStep,
    Ticket,
)
from app.persistence.mock_catalog import REFERENCE_TIME, build_orders_by_id
from app.services.payments import GatewayOutcome, SimulatedPaymentGateway
from app.tools.context import ToolContext
from tests.tools_util import build_tool_set, ctx as day2_ctx, make_approval

T0 = REFERENCE_TIME + timedelta(days=30)


def run(coro):
    return asyncio.run(coro)


def real_now() -> datetime:
    # RefundService checks approval validity against the wall clock.
    return datetime.now(timezone.utc)


def make_ticket(
    *,
    ticket_id: str,
    risk_level: RiskLevel,
    claimed_amount: Decimal,
    order_id: str = "ORD-1001",
    status: TicketStatus = TicketStatus.OPEN,
) -> Ticket:
    return Ticket(
        ticket_id=ticket_id,
        order_id=order_id,
        customer_id="CUS-1001",
        status=status,
        issue_type=IssueType.REFUND_REQUEST,
        risk_level=risk_level,
        subject=f"Regression ticket {ticket_id}",
        description="Regression test ticket for the P1 hardening fixes.",
        claimed_amount=claimed_amount,
        created_at=T0 - timedelta(days=1),
        updated_at=T0 - timedelta(days=1),
    )


def make_deps(*, tickets=None, orders=None, gateway=None):
    """Production wiring: the plan store is always injected, both into the
    graph and into the RefundService inside the ToolSet."""
    return build_agent_wiring(
        orders=orders or build_orders_by_id(),
        tickets=tickets,
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
        gateway=gateway,
        clock=lambda: T0,
    )


def hashless_approved(plan_id: str, *, requested_at=None) -> Approval:
    """Test fixture reproducing the V1 bypass shape: an APPROVED record
    with the correct binding fields but NO plan content hash."""
    return Approval(
        approval_id="APR-NOHASH",
        ticket_id="TKT-2005",
        order_id="ORD-1005",
        plan_id=plan_id,
        action="refund",
        amount=Decimal("129.90"),
        risk_level=RiskLevel.HIGH,
        plan_content_hash=None,
        justification="fixture approval without a plan content hash",
        status=ApprovalStatus.APPROVED,
        requested_at=requested_at or (T0 + timedelta(hours=1)),
        decided_at=(requested_at or (T0 + timedelta(hours=1))) + timedelta(hours=1),
        decided_by="fixture",
    )


# --- P1-1: a missing plan content hash fails closed ---


def test_graph_blocks_approval_without_plan_hash():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    real = deps.approvals.latest_for("TKT-2005")
    deps.tools.approvals.add(hashless_approved(real.plan_id))
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.FAILED
    assert outcome.error and "does not match the registered plan" in outcome.error
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.IN_PROGRESS


def test_service_rejects_approval_without_plan_hash():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    real = deps.approvals.latest_for("TKT-2005")
    deps.tools.approvals.add(hashless_approved(real.plan_id))
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-2005", run_id="RUN-TEST"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="p1-1-nohash",
            plan_id=real.plan_id,
        )
    )
    assert result.error_code.value == "APPROVAL_MISMATCH"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_service_rejects_approval_referencing_unregistered_plan():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    deps.tools.approvals.add(
        Approval(
            approval_id="APR-GHOST",
            ticket_id="TKT-2005",
            order_id="ORD-1005",
            plan_id="PLAN-ghost",
            action="refund",
            amount=Decimal("129.90"),
            risk_level=RiskLevel.HIGH,
            plan_content_hash="a" * 64,
            justification="fixture approval referencing an unregistered plan",
            status=ApprovalStatus.APPROVED,
            requested_at=T0 + timedelta(hours=1),
            decided_at=T0 + timedelta(hours=2),
            decided_by="fixture",
        )
    )
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-2005", run_id="RUN-TEST"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="p1-1-ghost",
            plan_id="PLAN-ghost",
        )
    )
    assert result.error_code.value == "APPROVAL_MISMATCH"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_service_rejects_approval_with_forged_binding_fields():
    """The hash still matches the registered plan (the plan was not
    modified); the forged approval binding field must be caught."""
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    real = deps.approvals.latest_for("TKT-2005")
    deps.tools.approvals.add(
        real.model_copy(
            update={
                "approval_id": "APR-FORGED",
                "order_id": "ORD-1001",  # forged: plan is bound to ORD-1005
                "status": ApprovalStatus.APPROVED,
                "requested_at": T0 + timedelta(hours=1),
                "decided_at": T0 + timedelta(hours=2),
                "decided_by": "fixture",
            }
        )
    )
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-2005", run_id="RUN-TEST"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="p1-1-forged",
            plan_id=real.plan_id,
        )
    )
    assert result.error_code.value == "APPROVAL_MISMATCH"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_day2_plans_less_fixture_path_is_preserved():
    """The hash-less plans-less construction is a Day 2 test-fixture path
    only; the production construction always injects a plan store, where a
    missing hash fails closed (see the tests above)."""
    ts = build_tool_set(approvals=(make_approval(),))
    result = run(
        ts.create_refund(
            day2_ctx(ticket_id="TKT-2005"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="day2-fixture-key",
            plan_id="PLAN-1",
        )
    )
    assert result.success
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED


# --- P1-2: the service enforces the risk-based approval rule itself ---


@pytest.mark.parametrize("risk_level", [RiskLevel.HIGH, RiskLevel.CRITICAL])
def test_service_rejects_unapproved_refund_at_high_or_critical_risk(risk_level):
    ticket = make_ticket(
        ticket_id="TKT-9001", risk_level=risk_level,
        claimed_amount=Decimal("199.99"),
    )
    deps = make_deps(tickets={"TKT-9001": ticket})
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9001", run_id="RUN-DIRECT"),
            order_id="ORD-1001",
            amount=Decimal("199.99"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key=f"p1-2-{risk_level.value}",
            plan_id=None,
        )
    )
    assert result.error_code.value == "APPROVAL_REQUIRED"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("0")
    assert order.reserved_amount == Decimal("0")


def test_service_executes_with_a_valid_hash_bound_approval():
    ticket = make_ticket(
        ticket_id="TKT-9001", risk_level=RiskLevel.HIGH,
        claimed_amount=Decimal("199.99"),
    )
    deps = make_deps(tickets={"TKT-9001": ticket})
    stored = deps.plans.register(
        ResolutionPlan(
            plan_id="PLAN-9001",
            ticket_id="TKT-9001",
            order_id="ORD-1001",
            issue_type=IssueType.REFUND_REQUEST,
            action="refund",
            amount=Decimal("199.99"),
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            steps=(ResolutionPlanStep(action="issue refund", reason="high-risk refund"),),
            created_at=T0,
        ),
        run_id="RUN-SETUP",
        created_at=T0,
    )
    approval = run(
        deps.approvals.request(
            plan=stored,
            action="refund",
            justification="high-risk refund needs human review",
            requested_at=T0,
            ctx=ToolContext(ticket_id="TKT-9001", run_id="RUN-SETUP"),
        )
    )
    run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=T0 + timedelta(hours=1),
            expires_at=real_now() + timedelta(days=7),
        )
    )
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9001", run_id="RUN-DIRECT"),
            order_id="ORD-1001",
            amount=Decimal("199.99"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="p1-2-valid",
            plan_id="PLAN-9001",
        )
    )
    assert result.success
    records = deps.tools.refund_store.all()
    assert len(records) == 1
    assert records[0].status is RefundStatus.PROCESSED
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("199.99")
    assert order.reserved_amount == Decimal("0")
    assert deps.tools.gateway.execute_calls == 1


def test_service_rejects_approval_with_forged_lower_risk():
    ticket = make_ticket(
        ticket_id="TKT-9002", risk_level=RiskLevel.CRITICAL,
        claimed_amount=Decimal("199.99"),
    )
    deps = make_deps(tickets={"TKT-9002": ticket})
    stored = deps.plans.register(
        ResolutionPlan(
            plan_id="PLAN-9002",
            ticket_id="TKT-9002",
            order_id="ORD-1001",
            issue_type=IssueType.REFUND_REQUEST,
            action="refund",
            amount=Decimal("199.99"),
            risk_level=RiskLevel.CRITICAL,
            requires_approval=True,
            steps=(ResolutionPlanStep(action="issue refund", reason="critical-risk refund"),),
            created_at=T0,
        ),
        run_id="RUN-SETUP",
        created_at=T0,
    )
    # Fixture: an APPROVED record for the same plan but a forged lower
    # risk level. The hash still matches (the plan was not modified); the
    # effective-risk check must refuse the under-level approval.
    deps.tools.approvals.add(
        Approval(
            approval_id="APR-FORGED-RISK",
            ticket_id="TKT-9002",
            order_id="ORD-1001",
            plan_id="PLAN-9002",
            action="refund",
            amount=Decimal("199.99"),
            risk_level=RiskLevel.HIGH,  # forged below the CRITICAL ticket
            plan_content_hash=stored.content_hash,
            justification="fixture approval with a forged lower risk level",
            status=ApprovalStatus.APPROVED,
            requested_at=T0 + timedelta(hours=1),
            decided_at=T0 + timedelta(hours=2),
            decided_by="fixture",
            expires_at=real_now() + timedelta(days=7),
        )
    )
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9002", run_id="RUN-DIRECT"),
            order_id="ORD-1001",
            amount=Decimal("199.99"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="p1-2-forged-risk",
            plan_id="PLAN-9002",
        )
    )
    assert result.error_code.value == "APPROVAL_MISMATCH"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


# --- P1-3: ticket-level claim dedup inside the atomic store section ---


def test_concurrent_runs_for_the_same_ticket_refund_at_most_once():
    ticket = make_ticket(
        ticket_id="TKT-9101", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"),
    )
    deps = make_deps(tickets={"TKT-9101": ticket})

    async def both():
        return await asyncio.gather(
            run_ticket("TKT-9101", deps), run_ticket("TKT-9101", deps)
        )

    a, b = run(both())
    records = deps.tools.refund_store.all()
    processed = [r for r in records if r.status is RefundStatus.PROCESSED]
    assert len(processed) == 1  # at most one side effect
    assert sum((r.amount for r in processed), Decimal("0")) == Decimal("50.00")
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("50.00")
    assert order.reserved_amount == Decimal("0")
    assert deps.tools.gateway.execute_calls == 1
    assert deps.tools.tickets.get("TKT-9101").status is TicketStatus.RESOLVED
    assert a.run_status is AgentRunStatus.COMPLETED
    assert b.run_status is AgentRunStatus.COMPLETED
    # The losing run refused with the claim-exceeded business rejection.
    losers = [o for o in (a, b) if o.final_reply and "claim" in o.final_reply]
    assert len(losers) == 1


def test_settled_refund_blocks_rerun_while_ticket_is_in_progress():
    """A PROCESSED refund whose ticket never reached RESOLVED (e.g. the
    run died between execute and finalize) blocks a fresh run with a new
    plan and a new idempotency key."""
    ticket = make_ticket(
        ticket_id="TKT-9102", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"), status=TicketStatus.IN_PROGRESS,
    )
    deps = make_deps(tickets={"TKT-9102": ticket})
    first = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9102", run_id="RUN-CRASHED"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="settled-key-1",
            plan_id="PLAN-9102",
        )
    )
    assert first.success
    outcome = run(run_ticket("TKT-9102", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "claim" in outcome.final_reply
    records = deps.tools.refund_store.all()
    assert len(records) == 1  # no second refund
    assert deps.tools.gateway.execute_calls == 1
    assert deps.tools.tickets.get("TKT-9102").status is TicketStatus.IN_PROGRESS


def test_unknown_refund_keeps_its_gate_and_same_key_resolution():
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.UNKNOWN])
    ticket = make_ticket(
        ticket_id="TKT-9103", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"),
    )
    deps = make_deps(tickets={"TKT-9103": ticket}, gateway=gateway)
    outcome = run(run_ticket("TKT-9103", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.UNKNOWN
    # A different key/plan for the same ticket is still blocked by the
    # unresolved gate with its original error code.
    result = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9103", run_id="RUN-OTHER"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="other-key",
            plan_id="PLAN-OTHER",
        )
    )
    assert result.error_code.value == "UNRESOLVED_REFUND"
    assert deps.tools.refund_store.count() == 1
    assert gateway.execute_calls == 1
    # The same key re-queries the original operation and settles it.
    settled = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9103", run_id="RUN-RESOLVE"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key=record.idempotency_key,
            plan_id=record.plan_id,
        )
    )
    assert settled.success
    assert deps.tools.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert gateway.execute_calls == 1  # no new execution
    assert gateway.status_calls == 1  # one status query


def test_other_tickets_on_the_same_order_refund_independently():
    ticket_a = make_ticket(
        ticket_id="TKT-9110", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"),
    )
    ticket_b = make_ticket(
        ticket_id="TKT-9111", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("30.00"),
    )
    deps = make_deps(tickets={"TKT-9110": ticket_a, "TKT-9111": ticket_b})
    for ticket_id, amount, key in (
        ("TKT-9110", "50.00", "refund-key-a"),
        ("TKT-9111", "30.00", "refund-key-b"),
    ):
        result = run(
            deps.tools.create_refund(
                ToolContext(ticket_id=ticket_id, run_id=f"RUN-{ticket_id}"),
                order_id="ORD-1001",
                amount=Decimal(amount),
                issue_type=IssueType.REFUND_REQUEST,
                idempotency_key=key,
                plan_id=None,
            )
        )
        assert result.success, ticket_id
    records = deps.tools.refund_store.all()
    assert len(records) == 2
    assert all(r.status is RefundStatus.PROCESSED for r in records)
    assert deps.tools.orders.get("ORD-1001").refunded_amount == Decimal("80.00")
    assert deps.tools.gateway.execute_calls == 2


def test_partial_refunds_within_the_claim_are_allowed_but_not_beyond():
    """Business semantics: multiple partial refunds per ticket are legal
    up to the ticket's claimed amount (the trusted claim identity); beyond
    it the store refuses inside the atomic section, even with a fresh
    plan id and idempotency key."""
    order = Order(
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal("500.00"),  # enough balance to isolate the claim cap
        created_at=T0 - timedelta(days=30),
        delivered_at=T0 - timedelta(days=20),
    )
    ticket = make_ticket(
        ticket_id="TKT-9112", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("199.99"),
    )
    deps = make_deps(tickets={"TKT-9112": ticket}, orders={"ORD-1001": order})
    for i, amount in enumerate(("50.00", "50.00", "99.99")):
        result = run(
            deps.tools.create_refund(
                ToolContext(ticket_id="TKT-9112", run_id=f"RUN-P{i}"),
                order_id="ORD-1001",
                amount=Decimal(amount),
                issue_type=IssueType.REFUND_REQUEST,
                idempotency_key=f"refund-key-p{i}",
                plan_id=f"PLAN-P{i}",
            )
        )
        assert result.success, amount
    over = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9112", run_id="RUN-P-OVER"),
            order_id="ORD-1001",
            amount=Decimal("0.01"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="key-p-over",
            plan_id="PLAN-P-OVER",
        )
    )
    assert over.error_code.value == "CLAIM_EXCEEDED"
    assert deps.tools.refund_store.count() == 3  # no fourth record
    order_after = deps.tools.orders.get("ORD-1001")
    assert order_after.refunded_amount == Decimal("199.99")
    assert order_after.reserved_amount == Decimal("0")
    assert deps.tools.gateway.execute_calls == 3
