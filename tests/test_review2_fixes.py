"""Regression tests for the second adversarial review round.

P1: retrying a FAILED refund re-checks the ticket claim cap atomically.
    Once another refund has occupied the claim, the original retry is
    refused with CLAIM_EXCEEDED before any reservation is taken or any
    gateway call is made; a retry while the claim is still free succeeds
    by reusing the same record.

P2: deterministic approval ordering on equal ``requested_at`` — one
    shared rule for ApprovalService and RefundService (the later-inserted
    record wins; a strictly later timestamp always wins); the approval
    request node loads the plan through ``load_verified`` and fails
    closed on drift; ticket status transitions are conditional and
    atomic, so a failed run can never pull a RESOLVED ticket backwards.

All tests are offline and assert persisted state — refund records,
balances, approvals, ticket status and gateway call counts — not reply
text. The concurrent-failure interleaving for ticket status is covered
with a deterministic node-level sequence (run A resolves, then run B's
failing finalize executes), not by repeating a concurrent run.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.agent.graph import SupportFlowAgent
from app.agent.llm import (
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring, run_ticket
from app.agent.state import PHASE_FATAL, PHASE_PENDING_APPROVAL
from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    OrderStatus,
    RefundStatus,
    RiskLevel,
    TicketStatus,
)
from app.domain.models import AgentRun, Order, Ticket
from app.persistence.memory_stores import ApprovalStore, TicketStore
from app.persistence.mock_catalog import REFERENCE_TIME, build_orders_by_id
from app.services.approvals import ApprovalService, latest_approval
from app.services.payments import GatewayOutcome, SimulatedPaymentGateway
from app.tools.context import ToolContext
from tests.tools_util import build_tool_set, ctx as day2_ctx, make_approval

T0 = REFERENCE_TIME + timedelta(days=30)


def run(coro):
    return asyncio.run(coro)


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
        description="Regression test ticket for the second review round.",
        claimed_amount=claimed_amount,
        created_at=T0 - timedelta(days=1),
        updated_at=T0 - timedelta(days=1),
    )


def make_order(*, order_id: str = "ORD-1001", total: str = "500.00") -> Order:
    return Order(
        order_id=order_id,
        customer_id="CUS-1001",
        status=OrderStatus.DELIVERED,
        currency="USD",
        total_amount=Decimal(total),  # enough balance to isolate the claim cap
        created_at=T0 - timedelta(days=30),
        delivered_at=T0 - timedelta(days=20),
    )


def make_deps(*, tickets=None, orders=None, gateway=None):
    """Production wiring over the mock catalog (or injected stores)."""
    return build_agent_wiring(
        orders=orders or build_orders_by_id(),
        tickets=tickets,
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
        gateway=gateway,
        clock=lambda: T0,
    )


def make_agent(deps) -> SupportFlowAgent:
    """The same node object the compiled graph runs, for deterministic
    node-level sequencing tests."""
    return SupportFlowAgent(
        tools=deps.tools,
        plans=deps.plans,
        approvals=deps.approvals,
        runs=deps.runs,
        recorder=deps.recorder,
        classifier=deps.classifier,
        planner=deps.planner,
        reply_generator=deps.reply_generator,
        llm_timeout=deps.llm_timeout,
        clock=deps.clock,
    )


def base_state(ticket_id: str, run_id: str, **extra) -> dict:
    state = {
        "ticket_id": ticket_id,
        "run_id": run_id,
        "max_iterations": 10,
        "iterations": 0,
    }
    state.update(extra)
    return state


# --- P1: FAILED retry re-checks the ticket claim cap atomically ---


def test_failed_refund_retry_is_blocked_by_the_ticket_claim_cap():
    """50 fails (reservation released), 60 succeeds, retrying the original
    50 with its own idempotency key must be refused: 50 + 60 would exceed
    the 100.00 claim. The refused reopen must not reserve balance, must
    not touch the gateway and must leave the FAILED record untouched."""
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED]
    )
    ticket = make_ticket(
        ticket_id="TKT-9200", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("100.00"),
    )
    deps = make_deps(
        tickets={"TKT-9200": ticket},
        orders={"ORD-1001": make_order()},
        gateway=gateway,
    )

    first = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9200", run_id="RUN-FIRST"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="retry-claim-50",
            plan_id="PLAN-A",
        )
    )
    assert first.error_code.value == "UPSTREAM_TIMEOUT"
    assert deps.tools.refund_store.all()[0].status is RefundStatus.FAILED

    second = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9200", run_id="RUN-SECOND"),
            order_id="ORD-1001",
            amount=Decimal("60.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="retry-claim-60",
            plan_id="PLAN-B",
        )
    )
    assert second.success

    # Retry the original operation with its own key: the reopen must be
    # refused inside the atomic section, before any reservation or call.
    retry = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9200", run_id="RUN-RETRY"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="retry-claim-50",
            plan_id="PLAN-A",
        )
    )
    assert retry.error_code.value == "CLAIM_EXCEEDED"

    records = deps.tools.refund_store.all()
    assert len(records) == 2  # no third record was created
    by_status = {r.status: r for r in records}
    assert by_status[RefundStatus.FAILED].amount == Decimal("50.00")
    assert by_status[RefundStatus.PROCESSED].amount == Decimal("60.00")
    processed_total = sum(
        (r.amount for r in records if r.status is RefundStatus.PROCESSED),
        Decimal("0"),
    )
    assert processed_total == Decimal("60.00")
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("60.00")
    assert order.reserved_amount == Decimal("0")  # no leaked reservation
    assert gateway.execute_calls == 2  # the refused retry never executed


def test_failed_refund_retry_succeeds_when_the_claim_is_still_free():
    """Control: the same FAILED record, retried with the same key while
    nothing else occupies the claim, reopens atomically and processes —
    reusing the original record, not creating a new one."""
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED]
    )
    ticket = make_ticket(
        ticket_id="TKT-9201", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("100.00"),
    )
    deps = make_deps(
        tickets={"TKT-9201": ticket},
        orders={"ORD-1001": make_order()},
        gateway=gateway,
    )

    first = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9201", run_id="RUN-FIRST"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="retry-ok-50",
            plan_id="PLAN-A",
        )
    )
    assert first.error_code.value == "UPSTREAM_TIMEOUT"

    retry = run(
        deps.tools.create_refund(
            ToolContext(ticket_id="TKT-9201", run_id="RUN-RETRY"),
            order_id="ORD-1001",
            amount=Decimal("50.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="retry-ok-50",
            plan_id="PLAN-A",
        )
    )
    assert retry.success
    records = deps.tools.refund_store.all()
    assert len(records) == 1  # the same record was reopened, not replaced
    assert records[0].status is RefundStatus.PROCESSED
    assert records[0].amount == Decimal("50.00")
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("50.00")
    assert order.reserved_amount == Decimal("0")
    assert gateway.execute_calls == 2


# --- P2: deterministic approval ordering on equal requested_at ---


def test_latest_approval_tie_goes_to_the_later_inserted_record():
    store = ApprovalStore()
    service = ApprovalService(store=store)
    tied = REFERENCE_TIME + timedelta(days=8)
    approved_a = make_approval(status=ApprovalStatus.APPROVED).model_copy(
        update={"approval_id": "APR-A", "requested_at": tied}
    )
    rejected_b = make_approval(status=ApprovalStatus.REJECTED).model_copy(
        update={"approval_id": "APR-B", "requested_at": tied}
    )
    store.add(approved_a)
    store.add(rejected_b)
    # Same timestamp: the later-inserted record wins, deterministically.
    assert service.latest_for("TKT-2005").approval_id == "APR-B"
    # A strictly later timestamp wins regardless of insertion order.
    pending_c = make_approval(status=ApprovalStatus.PENDING).model_copy(
        update={
            "approval_id": "APR-C",
            "requested_at": tied + timedelta(hours=1),
        }
    )
    store.add(pending_c)
    assert service.latest_for("TKT-2005").approval_id == "APR-C"
    # The shared pure function agrees with the service.
    assert latest_approval(store.all()).approval_id == "APR-C"
    assert latest_approval(()) is None


def test_refund_service_uses_the_same_tie_rule_rejected_wins():
    """Two approvals with the same requested_at: the later-inserted one is
    REJECTED, so the service must refuse the refund — proving the service
    and the approval service pick the same winner."""
    tied = REFERENCE_TIME + timedelta(days=8)
    approved = make_approval(status=ApprovalStatus.APPROVED).model_copy(
        update={"approval_id": "APR-A", "requested_at": tied}
    )
    rejected = make_approval(status=ApprovalStatus.REJECTED).model_copy(
        update={"approval_id": "APR-B", "requested_at": tied}
    )
    ts = build_tool_set(approvals=(approved, rejected))
    result = run(
        ts.create_refund(
            day2_ctx("TKT-2005"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="tie-svc-rej",
            plan_id="PLAN-1",
        )
    )
    assert result.error_code.value == "APPROVAL_REJECTED"
    assert ts.refund_store.count() == 0
    assert ts.gateway.execute_calls == 0


def test_refund_service_uses_the_same_tie_rule_approved_wins():
    """Reverse insertion order: the later-inserted record is APPROVED, so
    the same tie rule now unlocks execution — the winner is the insertion
    order, not a fixed status."""
    tied = REFERENCE_TIME + timedelta(days=8)
    rejected = make_approval(status=ApprovalStatus.REJECTED).model_copy(
        update={"approval_id": "APR-B", "requested_at": tied}
    )
    approved = make_approval(status=ApprovalStatus.APPROVED).model_copy(
        update={"approval_id": "APR-A", "requested_at": tied}
    )
    ts = build_tool_set(approvals=(rejected, approved))
    result = run(
        ts.create_refund(
            day2_ctx("TKT-2005"),
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="tie-svc-app",
            plan_id="PLAN-1",
        )
    )
    assert result.success
    assert ts.refund_store.all()[0].status is RefundStatus.PROCESSED
    assert ts.gateway.execute_calls == 1


# --- P2: request_approval re-verifies the plan (load_verified) ---


def test_request_approval_fails_closed_on_plan_drift():
    """The approval-request node must load the plan through load_verified:
    white-box drift of the stored content (same plan_id, same recorded
    hash) makes the request fail closed before any approval is created."""
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))  # registers the plan, requests approval
    plan_id = deps.approvals.latest_for("TKT-2005").plan_id
    stored = deps.plans.get(plan_id)
    assert stored is not None

    # White-box drift: replace the stored plan with different content that
    # still carries the original recorded hash.
    deps.plans._by_id[plan_id] = stored.model_copy(
        update={
            "content": stored.content.model_copy(
                update={"amount": Decimal("1.00")}
            )
        }
    )

    agent = make_agent(deps)
    result = run(
        agent.request_approval(
            base_state(
                "TKT-2005",
                "RUN-DRIFT",
                plan=stored.content,
                plan_id=plan_id,
            )
        )
    )
    assert result["phase"] == PHASE_FATAL
    assert "integrity" in result["error"]
    # No second approval request, no ticket change, no refund, no gateway.
    assert len(deps.tools.approvals.all()) == 1
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.PENDING_APPROVAL
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_request_approval_succeeds_with_a_verified_plan():
    """Control: with the stored plan intact, load_verified returns it and
    the node requests the approval (idempotent on repeat)."""
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    plan_id = deps.approvals.latest_for("TKT-2005").plan_id
    stored = deps.plans.load_verified(plan_id)
    assert stored is not None

    agent = make_agent(deps)
    result = run(
        agent.request_approval(
            base_state(
                "TKT-2005",
                "RUN-CONTROL",
                plan=stored.content,
                plan_id=plan_id,
            )
        )
    )
    assert result["phase"] == PHASE_PENDING_APPROVAL
    assert len(deps.tools.approvals.all()) == 1  # idempotent per (ticket, plan)
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.PENDING_APPROVAL
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


# --- P2: ticket status can never regress (atomic conditional transitions) ---


def test_failed_finalize_cannot_pull_a_resolved_ticket_back():
    """Deterministic interleaving of the concurrency hazard: run A resolves
    the ticket, then run B's failing finalize executes. The IN_PROGRESS
    transition must be refused and the audit must report the actual final
    status (RESOLVED)."""
    deps = make_deps()
    run(run_ticket("TKT-2001", deps))  # run A: refund succeeds, ticket RESOLVED
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.RESOLVED

    # Run B was in flight and fails after run A finished.
    deps.runs.create(
        AgentRun(
            run_id="RUN-LOSER",
            ticket_id="TKT-2001",
            status=AgentRunStatus.RUNNING,
            started_at=T0,
            max_iterations=10,
        )
    )
    agent = make_agent(deps)
    result = run(
        agent.finalize(
            base_state(
                "TKT-2001",
                "RUN-LOSER",
                iterations=1,
                phase=PHASE_FATAL,
                error="iteration limit reached before execution",
            )
        )
    )
    assert result["run_status"] is AgentRunStatus.FAILED
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.RESOLVED
    finished = [
        e for e in deps.tools.audit_store.all() if e.action == "agent_run_finished"
    ]
    assert finished[-1].details["ticket_status"] == "resolved"


def test_failed_finalize_still_moves_an_open_ticket_to_in_progress():
    """The guard must not over-block: a failed run on an OPEN ticket still
    parks it at IN_PROGRESS as before."""
    ticket = make_ticket(
        ticket_id="TKT-9301", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"),
    )
    deps = make_deps(tickets={"TKT-9301": ticket})
    deps.runs.create(
        AgentRun(
            run_id="RUN-B",
            ticket_id="TKT-9301",
            status=AgentRunStatus.RUNNING,
            started_at=T0,
            max_iterations=10,
        )
    )
    agent = make_agent(deps)
    result = run(
        agent.finalize(
            base_state(
                "TKT-9301",
                "RUN-B",
                iterations=1,
                phase=PHASE_FATAL,
                error="boom",
            )
        )
    )
    assert result["run_status"] is AgentRunStatus.FAILED
    assert deps.tools.tickets.get("TKT-9301").status is TicketStatus.IN_PROGRESS


def test_transition_status_refuses_regressions_atomically():
    """Store-level guard: the transition out of a terminal state returns
    None and changes nothing; an allowed transition still applies."""
    active_from = (
        TicketStatus.OPEN,
        TicketStatus.IN_PROGRESS,
        TicketStatus.PENDING_APPROVAL,
    )
    resolved = make_ticket(
        ticket_id="TKT-9302", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"), status=TicketStatus.RESOLVED,
    )
    store = TicketStore({"TKT-9302": resolved})
    moved = store.transition_status(
        "TKT-9302", TicketStatus.IN_PROGRESS, now=T0, allowed_from=active_from
    )
    assert moved is None
    assert store.get("TKT-9302").status is TicketStatus.RESOLVED

    open_ticket = make_ticket(
        ticket_id="TKT-9303", risk_level=RiskLevel.LOW,
        claimed_amount=Decimal("50.00"),
    )
    store2 = TicketStore({"TKT-9303": open_ticket})
    moved = store2.transition_status(
        "TKT-9303", TicketStatus.IN_PROGRESS, now=T0, allowed_from=active_from
    )
    assert moved is not None
    assert moved.status is TicketStatus.IN_PROGRESS
    assert store2.get("TKT-9303").status is TicketStatus.IN_PROGRESS
