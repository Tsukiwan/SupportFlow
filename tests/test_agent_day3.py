"""Day 3 agent tests: graph behavior, approval binding, execution safety.

Every scenario asserts on persisted state — tool call counts, refund
records, balances, approvals, ticket status and the audit trail — not just
on reply text. All collaborators are the deterministic offline defaults or
explicit fakes; no test reaches the network or requires an API key.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.agent.llm import (
    ClassificationProposal,
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring, run_ticket
from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    RefundStatus,
    RiskLevel,
    TicketStatus,
)
from app.domain.models import Approval, ResolutionPlan, ResolutionPlanStep, Ticket
from app.persistence.mock_catalog import REFERENCE_TIME, build_orders_by_id
from app.services.payments import GatewayOutcome, SimulatedPaymentGateway
from app.tools.context import ToolContext
from app.tools.result import ToolErrorCode

# Fixed clock AFTER every catalog timestamp (the newest is REFERENCE_TIME
# + 10 days): init_run rewrites ticket.updated_at with the clock, and the
# Ticket validator rejects a timestamp that precedes created_at.
T0 = REFERENCE_TIME + timedelta(days=30)


def run(coro):
    return asyncio.run(coro)


def real_now() -> datetime:
    # RefundService checks approval validity against the wall clock, so
    # approval fixtures that must stay valid use real time, not T0.
    return datetime.now(timezone.utc)


def make_deps(
    *, gateway=None, classifier=None, planner=None, reply_generator=None, clock=None
):
    """Wiring over the deterministic catalog with offline collaborators."""
    return build_agent_wiring(
        gateway=gateway,
        classifier=classifier or DefaultClassifier(),
        planner=planner or DefaultPlanProposer(),
        reply_generator=reply_generator or DefaultReplyGenerator(),
        clock=clock or (lambda: T0),
    )


def actions(deps) -> list[str]:
    return [e.action for e in deps.tools.audit_store.all()]


def events(deps, action: str):
    return [e for e in deps.tools.audit_store.all() if e.action == action]


def refund_plan(
    *, order_id: str = "ORD-1001", amount: Decimal = Decimal("199.99"),
    ticket_id: str = "TKT-2001",
) -> ResolutionPlan:
    return ResolutionPlan(
        plan_id="draft",
        ticket_id=ticket_id,
        order_id=order_id,
        issue_type=IssueType.REFUND_REQUEST,
        action="refund",
        amount=amount,
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        steps=(ResolutionPlanStep(action="issue refund", reason="customer request"),),
        created_at=T0,
    )


# --- adversarial collaborators (all offline) ---


class FakeClassifier:
    def __init__(self, issue_type, risk_level):
        self.proposal = ClassificationProposal(
            issue_type=issue_type, risk_level=risk_level, note="adversarial proposal"
        )

    async def classify(self, ticket):
        return self.proposal


class FixedPlanPlanner:
    def __init__(self, plan: ResolutionPlan):
        self.plan = plan

    async def propose(self, ctx):
        return self.plan


class CountingPlanner:
    """Valid proposal for the initial run; must not be consulted again on
    resume, when the graph reuses the approval-bound stored plan."""

    def __init__(self):
        self.calls = 0

    async def propose(self, ctx):
        self.calls += 1
        return refund_plan(
            ticket_id=ctx.ticket.ticket_id,
            order_id=ctx.ticket.order_id,
            amount=ctx.ticket.claimed_amount or Decimal("129.90"),
        )


class RaisingPlanner:
    async def propose(self, ctx):
        raise RuntimeError("model returned an invalid plan structure")


class RaisingClassifier:
    async def classify(self, ticket):
        raise RuntimeError("model unavailable")


class NotePlanPlanner:
    async def propose(self, ctx):
        return ResolutionPlan(
            plan_id="draft",
            ticket_id=ctx.ticket.ticket_id,
            order_id=None,
            issue_type=ctx.ticket.issue_type,
            action="note",
            risk_level=ctx.trusted_risk_level,
            requires_approval=False,
            steps=(
                ResolutionPlanStep(action="leave note", reason="escalate for human review"),
            ),
            created_at=T0,
        )


class BoomReplyGenerator:
    async def generate(self, facts):
        raise RuntimeError("reply model failed")


class ConflictingReplyGenerator:
    """Returns text that contradicts the verified refund outcome."""

    async def generate(self, facts):
        return "Your refund has been processed successfully."


def inquiry_deps(*, planner=None):
    ticket = Ticket(
        ticket_id="TKT-3001",
        order_id="ORD-1001",
        customer_id="CUS-1001",
        status=TicketStatus.OPEN,
        issue_type=IssueType.ORDER_INQUIRY,
        risk_level=RiskLevel.LOW,
        subject="Where is my order",
        description="Customer asks for the current status of the order.",
        created_at=T0,
        updated_at=T0,
    )
    return build_agent_wiring(
        orders=build_orders_by_id(),
        tickets={"TKT-3001": ticket},
        classifier=DefaultClassifier(),
        planner=planner or DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
        clock=lambda: T0,
    )


# --- 1. ordinary consultation completes end to end ---


def test_consultation_refund_completes_successfully():
    deps = make_deps()
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.phase == "completed"
    assert outcome.iterations == 1
    assert outcome.final_reply and "199.99" in outcome.final_reply
    assert "processed" in outcome.final_reply
    records = deps.tools.refund_store.all()
    assert len(records) == 1
    record = records[0]
    assert record.status is RefundStatus.PROCESSED
    assert record.amount == Decimal("199.99")
    assert record.plan_id == outcome.plan_id
    assert record.idempotency_key == f"agent-refund-TKT-2001-{outcome.plan_id}"
    order = deps.tools.orders.get("ORD-1001")
    assert order.refunded_amount == Decimal("199.99")
    assert order.reserved_amount == Decimal("0")
    assert deps.tools.gateway.execute_calls == 1
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.RESOLVED
    assert len(deps.tools.approvals.all()) == 0  # low risk: no approval needed
    trail = actions(deps)
    for action in (
        "agent_run_started",
        "agent_classify",
        "agent_context",
        "agent_plan_proposed",
        "agent_plan_validated",
        "agent_approval_gate",
        "agent_execute",
        "create_refund",
        "agent_verify",
        "agent_reply",
        "agent_run_finished",
    ):
        assert action in trail, action
    # Planning and execution are distinct audit stages; the plan was
    # validated before the refund tool was called, and verified after.
    assert trail.index("agent_plan_validated") < trail.index("create_refund")
    assert trail.index("create_refund") < trail.index("agent_verify")
    validated = events(deps, "agent_plan_validated")[0]
    assert validated.details["action"] == "refund"
    assert validated.details["plan_content_hash"]


# --- 2. nonexistent order ---


def test_nonexistent_order_refuses_without_side_effects():
    deps = make_deps()
    outcome = run(run_ticket("TKT-2006", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "not found" in outcome.final_reply
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert "create_refund" not in actions(deps)
    assert deps.tools.tickets.get("TKT-2006").status is TicketStatus.IN_PROGRESS


# --- 3. high-risk ticket cannot be relabeled as low risk ---


def test_high_risk_ticket_cannot_be_relabeled_as_low_risk():
    deps = make_deps(
        classifier=FakeClassifier(
            issue_type=IssueType.REFUND_REQUEST, risk_level=RiskLevel.LOW
        )
    )
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.AWAITING_APPROVAL
    classify = events(deps, "agent_classify")[0]
    assert classify.details["proposal_mismatch"] is True
    assert classify.details["trusted_issue_type"] == "delivery_issue"
    assert classify.details["trusted_risk_level"] == "high"
    approval = deps.approvals.latest_for("TKT-2005")
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    assert approval.risk_level is RiskLevel.HIGH  # the trusted record won
    assert approval.plan_content_hash
    stored = deps.plans.all()
    assert len(stored) == 1
    assert stored[0].content.risk_level is RiskLevel.HIGH
    assert stored[0].content.issue_type is IssueType.DELIVERY_ISSUE
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.PENDING_APPROVAL


# --- 4. plan binding: wrong order / wrong amount are rejected ---


def test_plan_for_a_different_order_is_rejected():
    deps = make_deps(planner=FixedPlanPlanner(refund_plan(order_id="ORD-9999")))
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "not bound" in outcome.final_reply
    validated = events(deps, "agent_plan_validated")[0]
    assert validated.details["outcome"] == "business_rejection"
    assert validated.details["reply_kind"] == "plan_order_mismatch"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_plan_amount_diverging_from_the_ticket_claim_is_rejected():
    deps = make_deps(planner=FixedPlanPlanner(refund_plan(amount=Decimal("500.00"))))
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "does not match the ticket claim" in outcome.final_reply
    validated = events(deps, "agent_plan_validated")[0]
    assert validated.details["reply_kind"] == "plan_amount_mismatch"
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


# --- 5/6. high-risk plans pause at the approval gate ---


def test_high_risk_plan_pauses_for_approval():
    deps = make_deps()
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.AWAITING_APPROVAL
    assert outcome.phase == "pending_approval"
    approval = deps.approvals.latest_for("TKT-2005")
    assert approval is not None and approval.status is ApprovalStatus.PENDING
    stored = deps.plans.get(approval.plan_id)
    assert stored is not None
    assert approval.plan_content_hash == stored.content_hash
    assert approval.order_id == "ORD-1005"
    assert approval.amount == Decimal("129.90")
    ticket = deps.tools.tickets.get("TKT-2005")
    assert ticket.status is TicketStatus.PENDING_APPROVAL  # paused, never resolved


def test_no_refund_tool_is_called_before_a_decision():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    assert "create_refund" not in actions(deps)
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.refund_store.count() == 0


# --- 7. rejection never executes ---


def test_rejected_approval_never_executes():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    approval = deps.approvals.latest_for("TKT-2005")
    run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.REJECTED,
            decided_by="bob",
            decided_at=T0 + timedelta(hours=1),
        )
    )
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "rejected" in outcome.final_reply
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert "create_refund" not in actions(deps)
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.IN_PROGRESS
    gate = events(deps, "agent_approval_gate")
    assert any(e.details["gate"] == "rejected" for e in gate)


# --- 8. approval/plan hash mismatch fails closed ---


def test_approval_with_tampered_plan_hash_blocks_execution():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    approval = deps.approvals.latest_for("TKT-2005")
    # Test fixture only: an APPROVED record whose plan hash does not match
    # the registered plan. The production path to APPROVED goes through
    # ApprovalService.decide; this fixture simulates a tampered record.
    deps.tools.approvals.add(
        Approval(
            approval_id="APR-FIXTURE",
            ticket_id="TKT-2005",
            order_id="ORD-1005",
            plan_id=approval.plan_id,
            action="refund",
            amount=Decimal("129.90"),
            risk_level=RiskLevel.HIGH,
            plan_content_hash="0" * 64,
            justification="test fixture: hash deliberately tampered",
            status=ApprovalStatus.APPROVED,
            requested_at=T0 + timedelta(hours=1),
            decided_at=T0 + timedelta(hours=2),
            decided_by="fixture",
        )
    )
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.FAILED
    assert outcome.error and "does not match the registered plan" in outcome.error
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.IN_PROGRESS


# --- 9. a modified amount cannot ride on the old approval ---


def test_resume_executes_the_approved_plan_ignoring_new_proposals():
    planner = CountingPlanner()
    deps = make_deps(planner=planner)
    run(run_ticket("TKT-2005", deps))
    approval = deps.approvals.latest_for("TKT-2005")
    run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=T0 + timedelta(hours=1),
            expires_at=real_now() + timedelta(days=7),
        )
    )
    outcome = run(run_ticket("TKT-2005", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.plan_id == approval.plan_id  # the plan the human approved
    records = deps.tools.refund_store.all()
    assert len(records) == 1
    assert records[0].amount == Decimal("129.90")  # not the LLM's new proposal
    # Consulted exactly once, for the initial proposal: the resume reused
    # the stored plan instead of asking the model for a new one.
    assert planner.calls == 1
    assert len(deps.plans.all()) == 1  # no replacement plan was registered
    assert deps.tools.gateway.execute_calls == 1
    assert "create_refund.approval_verified" in actions(deps)
    assert deps.tools.tickets.get("TKT-2005").status is TicketStatus.RESOLVED


def test_refund_service_rejects_amount_differing_from_the_approval():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    approval = deps.approvals.latest_for("TKT-2005")
    run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=T0 + timedelta(hours=1),
            expires_at=real_now() + timedelta(days=7),
        )
    )
    ctx = ToolContext(ticket_id="TKT-2005", run_id="RUN-TEST")
    # Same plan id, but a different amount than the approval covers.
    result = run(
        deps.tools.create_refund(
            ctx,
            order_id="ORD-1005",
            amount=Decimal("500.00"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="test-key-1",
            plan_id=approval.plan_id,
        )
    )
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.refund_store.count() == 0
    # A wrong plan version is likewise refused at the service boundary.
    result = run(
        deps.tools.create_refund(
            ctx,
            order_id="ORD-1005",
            amount=Decimal("129.90"),
            issue_type=IssueType.DELIVERY_ISSUE,
            idempotency_key="test-key-2",
            plan_id="PLAN-other",
        )
    )
    assert result.error_code is ToolErrorCode.APPROVAL_MISMATCH
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.refund_store.count() == 0


# --- 10. reply failure after a successful refund never re-executes it ---


def test_reply_failure_uses_template_without_re_executing_refund():
    deps = make_deps(reply_generator=BoomReplyGenerator())
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "processed" in outcome.final_reply
    assert deps.tools.refund_store.count() == 1
    assert deps.tools.gateway.execute_calls == 1
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.PROCESSED
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.RESOLVED
    reply_events = events(deps, "agent_reply")
    assert reply_events and reply_events[0].details["used_template"] is True


# --- 11/12. gateway UNKNOWN: no blind retry, new refunds blocked ---


def test_gateway_unknown_does_not_retry_and_leaves_ticket_unresolved():
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.UNKNOWN])
    deps = make_deps(gateway=gateway)
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "confirm" in outcome.final_reply
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.UNKNOWN
    order = deps.tools.orders.get("ORD-1001")
    assert order.reserved_amount == Decimal("199.99")  # fail closed: reservation kept
    assert order.refunded_amount == Decimal("0")
    assert gateway.execute_calls == 1
    assert gateway.status_calls == 0  # no blind re-query loop
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.IN_PROGRESS
    create_events = events(deps, "create_refund")
    assert create_events and create_events[0].details["outcome"] == "unknown"


def test_unresolved_refund_blocks_a_new_refund_for_the_order():
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.UNKNOWN])
    deps = make_deps(gateway=gateway)
    first = run(run_ticket("TKT-2001", deps))
    assert first.run_status is AgentRunStatus.COMPLETED
    second = run(run_ticket("TKT-2001", deps))
    assert second.run_status is AgentRunStatus.COMPLETED
    assert second.final_reply and "cannot" in second.final_reply
    assert deps.tools.refund_store.count() == 1  # still the original record
    assert gateway.execute_calls == 1  # blocked before the gateway
    assert gateway.status_calls == 0
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.IN_PROGRESS


# --- 13. hard iteration limit terminates transient retries safely ---


def test_iteration_limit_terminates_transient_retries_safely():
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE] * 10
    )
    deps = make_deps(gateway=gateway)
    outcome = run(run_ticket("TKT-2001", deps, max_iterations=3))
    assert outcome.run_status is AgentRunStatus.FAILED
    assert outcome.error and "iteration limit" in outcome.error
    assert outcome.iterations == 3
    assert gateway.execute_calls == 3
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.FAILED
    order = deps.tools.orders.get("ORD-1001")
    assert order.reserved_amount == Decimal("0")  # released after each failure
    assert order.refunded_amount == Decimal("0")
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.IN_PROGRESS
    assert outcome.final_reply is None  # no success reply was produced
    finished = events(deps, "agent_run_finished")[0]
    assert finished.details["run_status"] == "failed"


# --- 14. invalid model output: bounded retries, then safe fallback ---


def test_invalid_model_output_retries_bounded_times_then_falls_back():
    deps = make_deps(planner=RaisingPlanner())
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    proposed = events(deps, "agent_plan_proposed")[0]
    assert proposed.details["retries"] == 2  # bounded attempts before fallback
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.PROCESSED
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.RESOLVED


def test_classifier_failure_falls_back_to_the_ticket_record():
    deps = make_deps(classifier=RaisingClassifier())
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    classify = events(deps, "agent_classify")[0]
    assert classify.details["outcome"] == "execution_failure"
    assert classify.details["trusted_issue_type"] == "refund_request"
    assert deps.tools.refund_store.count() == 1


# --- 15. reply text conflicting with verified results is replaced ---


def test_conflicting_reply_is_replaced_by_verified_results():
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.REJECTED])
    deps = make_deps(
        gateway=gateway, reply_generator=ConflictingReplyGenerator()
    )
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "rejected" in outcome.final_reply
    assert "processed successfully" not in outcome.final_reply.lower()
    record = deps.tools.refund_store.all()[0]
    assert record.status is RefundStatus.REJECTED
    order = deps.tools.orders.get("ORD-1001")
    assert order.reserved_amount == Decimal("0")  # released on rejection
    assert order.refunded_amount == Decimal("0")
    assert deps.tools.tickets.get("TKT-2001").status is TicketStatus.IN_PROGRESS
    reply_events = events(deps, "agent_reply")[0]
    assert reply_events.details["used_template"] is True


# --- 16. the agent can never create or decide approvals ---


def test_agent_sources_never_construct_or_decide_approvals():
    """Static guard: the agent runtime has no path to APPROVED records or
    to the decide entry; it also never mutates refund state directly."""
    import pathlib

    sources = {
        path.name: path.read_text()
        for path in pathlib.Path("app/agent").glob("*.py")
        if path.name in ("graph.py", "runner.py", "state.py", "llm.py")
    }
    for name, source in sources.items():
        assert "ApprovalStatus.APPROVED" not in source, name
        assert ".decide(" not in source, name
        assert "Approval(" not in source, name
        assert "refund_store.finalize(" not in source, name
        assert "refund_store.reopen(" not in source, name
        assert "refund_store.settle(" not in source, name


def test_running_twice_without_a_decision_never_approves():
    deps = make_deps()
    run(run_ticket("TKT-2005", deps))
    run(run_ticket("TKT-2005", deps))
    assert all(
        a.status is ApprovalStatus.PENDING for a in deps.tools.approvals.all()
    )
    assert "approval_decision" not in actions(deps)
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


# --- additional guardrails ---


def test_resolved_ticket_blocks_repeated_execution():
    deps = make_deps()
    run(run_ticket("TKT-2001", deps))
    outcome = run(run_ticket("TKT-2001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "already resolved" in outcome.final_reply
    assert deps.tools.refund_store.count() == 1
    assert deps.tools.gateway.execute_calls == 1  # the refund ran exactly once


def test_expired_approval_requires_a_fresh_decision():
    now = [T0]
    deps = make_deps(clock=lambda: now[0])
    run(run_ticket("TKT-2005", deps))
    approval = deps.approvals.latest_for("TKT-2005")
    run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=now[0] + timedelta(seconds=1),
            expires_at=now[0] + timedelta(seconds=1),
        )
    )
    now[0] = now[0] + timedelta(seconds=2)
    resumed = run(run_ticket("TKT-2005", deps))
    assert resumed.run_status is AgentRunStatus.AWAITING_APPROVAL
    approvals = deps.tools.approvals.all()
    assert len(approvals) == 2  # the old decision plus a fresh PENDING request
    latest = deps.approvals.latest_for("TKT-2005")
    assert latest.status is ApprovalStatus.PENDING
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0


def test_inquiry_without_refund_answers_from_trusted_facts():
    deps = inquiry_deps()
    outcome = run(run_ticket("TKT-3001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "delivered" in outcome.final_reply
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-3001").status is TicketStatus.RESOLVED


def test_note_action_records_and_resolves():
    deps = inquiry_deps(planner=NotePlanPlanner())
    outcome = run(run_ticket("TKT-3001", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    notes = deps.tools.notes.list_for("TKT-3001")
    assert len(notes) == 1
    assert "leave note" in notes[0].text
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-3001").status is TicketStatus.RESOLVED


def test_fully_refunded_order_is_refused():
    deps = make_deps()
    outcome = run(run_ticket("TKT-2002", deps))
    assert outcome.run_status is AgentRunStatus.COMPLETED
    assert outcome.final_reply and "already refunded" in outcome.final_reply
    assert deps.tools.refund_store.count() == 0
    assert deps.tools.gateway.execute_calls == 0
    assert deps.tools.tickets.get("TKT-2002").status is TicketStatus.IN_PROGRESS
