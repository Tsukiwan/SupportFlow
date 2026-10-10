"""SupportFlow agent graph: fixed, server-driven nodes with bounded loops.

Pipeline: init -> classify -> gather -> select_plan -> validate_plan,
then either request_approval (pause) or execute -> verify -> reply ->
finalize. The only loop is the bounded execute/verify retry cycle for
confirmed-transient failures; every other transition moves forward.

Safety properties the graph enforces:

- the LLM never calls tools: it only produces advisory classification,
  plan and reply proposals, all of which the graph validates against
  trusted stores before acting;
- execution loads the plan from the PlanStore (hash re-verified) and
  calls only the audited ToolSet methods — the graph has no access to
  refund state mutation, balance mutation, gateway state or approval
  decisions;
- approvals are requested here but only *decided* by the human-facing
  ApprovalService outside the graph; an APPROVED decision is re-verified
  (status, validity window, plan hash and bound fields) both here and
  inside RefundService before any refund executes;
- UNKNOWN gateway results are never blindly retried: the run ends as
  "needs verification" and only a same-key retry re-queries the original
  operation; UNKNOWN/PENDING refunds block new refunds for the order;
- a ticket becomes RESOLVED only when a verified business-completion
  condition holds (refund PROCESSED and verified, or a note/reply-only
  resolution was delivered); awaiting-approval and failed runs never
  resolve tickets.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.agent.llm import (
    ClassificationProposal,
    DefaultPlanProposer,
    PlanContext,
    PlanProposer,
    ReplyFacts,
    ReplyGenerator,
    TicketClassifier,
    verify_reply,
)
from app.agent.state import (
    PHASE_AWAIT_APPROVAL,
    PHASE_BUSINESS_REJECTION,
    PHASE_CONTINUE,
    PHASE_FATAL,
    PHASE_NEEDS_VERIFICATION,
    PHASE_PENDING_APPROVAL,
    PHASE_RETRY,
    PHASE_SUCCESS,
    PHASE_UNKNOWN,
    PHASE_UNRESOLVED,
    PHASE_VERIFIED,
    AgentState,
)
from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    RefundStatus,
    RISK_SEVERITY,
    RiskLevel,
    TicketStatus,
)
from app.domain.models import AgentRun, Approval, ResolutionPlan
from app.persistence.memory_stores import AgentRunStore
from app.services.approvals import ApprovalAgentPort
from app.services.audit import AuditRecorder
from app.services.plans import PlanStore
from app.services.refund_policy import approval_requirement
from app.tools.context import ToolContext
from app.tools.registry import ToolSet
from app.tools.result import ToolErrorCode

logger = logging.getLogger(__name__)

_PLAN_PROPOSAL_ATTEMPTS = 2  # bounded retries before the deterministic fallback


class SupportFlowAgent:
    def __init__(
        self,
        *,
        tools: ToolSet,
        plans: PlanStore,
        approvals: ApprovalAgentPort,
        runs: AgentRunStore,
        recorder: AuditRecorder,
        classifier: TicketClassifier,
        planner: PlanProposer,
        reply_generator: ReplyGenerator,
        llm_timeout: float = 30.0,
        clock=None,
    ) -> None:
        self.tools = tools
        self.plans = plans
        self.approvals = approvals
        self.runs = runs
        self.recorder = recorder
        self.classifier = classifier
        self.planner = planner
        self.reply_generator = reply_generator
        self.llm_timeout = llm_timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- helpers ---

    def _ctx(self, state: AgentState) -> ToolContext:
        return ToolContext(
            ticket_id=state["ticket_id"], run_id=state["run_id"]
        )

    async def _audit(
        self,
        state: AgentState,
        action: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.recorder.record(
            tool=action,
            ctx=self._ctx(state),
            outcome=outcome,
            details=details,
        )

    # --- nodes ---

    async def init_run(self, state: AgentState) -> dict:
        ticket_id = state["ticket_id"]
        now = self._clock()
        ticket = self.tools.tickets.get(ticket_id)
        run = AgentRun(
            run_id=state["run_id"],
            ticket_id=ticket_id,
            status=AgentRunStatus.RUNNING,
            started_at=now,
            max_iterations=state["max_iterations"],
        )
        self.runs.create(run)
        if ticket is None:
            await self._audit(
                state,
                "agent_run_started",
                "business_rejection",
                {"reason": "ticket not found"},
            )
            return {
                "ticket": None,
                "iterations": 0,
                "phase": PHASE_BUSINESS_REJECTION,
                "reply_kind": "ticket_not_found",
                "error": f"ticket {ticket_id} not found",
            }
        if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            await self._audit(
                state,
                "agent_run_started",
                "business_rejection",
                {"ticket_status": ticket.status.value},
            )
            return {
                "ticket": ticket,
                "iterations": 0,
                "phase": PHASE_BUSINESS_REJECTION,
                "reply_kind": "already_resolved",
            }
        if ticket.status is TicketStatus.OPEN:
            self.tools.tickets.transition_status(
                ticket_id,
                TicketStatus.IN_PROGRESS,
                now=now,
                allowed_from=(TicketStatus.OPEN,),
            )
            ticket = self.tools.tickets.get(ticket_id)
        await self._audit(
            state,
            "agent_run_started",
            "success",
            {"ticket_status": ticket.status.value},
        )
        return {"ticket": ticket, "iterations": 0}

    async def classify(self, state: AgentState) -> dict:
        ticket = state["ticket"]
        trusted_issue, trusted_risk = ticket.issue_type, ticket.risk_level
        try:
            proposal = await asyncio.wait_for(
                self.classifier.classify(ticket), timeout=self.llm_timeout
            )
        except Exception:
            # The LLM proposal is advisory; its failure must never block
            # the run. Fall back to the trusted record and say so.
            logger.exception("classifier failed; using the ticket record")
            proposal = ClassificationProposal(
                issue_type=trusted_issue,
                risk_level=trusted_risk,
                note="classifier failed; ticket record used",
            )
            outcome = "execution_failure"
        else:
            outcome = "success"
        mismatched = (
            proposal.issue_type is not trusted_issue
            or proposal.risk_level is not trusted_risk
        )
        await self._audit(
            state,
            "agent_classify",
            outcome,
            {
                "proposed_issue_type": (
                    proposal.issue_type.value if proposal.issue_type else None
                ),
                "proposed_risk_level": (
                    proposal.risk_level.value if proposal.risk_level else None
                ),
                "trusted_issue_type": trusted_issue.value,
                "trusted_risk_level": trusted_risk.value,
                "proposal_mismatch": mismatched,
            },
        )
        return {
            "classification_proposal": proposal.model_dump(mode="json"),
            "trusted_issue_type": trusted_issue,
            "trusted_risk_level": trusted_risk,
        }

    async def gather_context(self, state: AgentState) -> dict:
        ticket = state["ticket"]
        ctx = self._ctx(state)
        order_result = await self.tools.get_order(
            ctx, order_id=ticket.order_id
        )
        order = None
        if order_result.success:
            order = order_result.data
        elif order_result.error_code is not None and order_result.error_code.value == "INTERNAL_ERROR":
            await self._audit(state, "agent_context", "execution_failure", {})
            return {
                "phase": PHASE_FATAL,
                "error": f"order lookup failed: {order_result.message}",
            }
        shipment = None
        policy = None
        if order is not None:
            shipment_result = await self.tools.get_shipping_status(
                ctx, order_id=ticket.order_id
            )
            if shipment_result.success:
                shipment = shipment_result.data
            policy_result = await self.tools.get_refund_policy(
                ctx,
                issue_type=ticket.issue_type,
                order_status=order.status,
            )
            policy = policy_result.data
        await self._audit(
            state,
            "agent_context",
            "success",
            {
                "order_found": order is not None,
                "order_status": order.status.value if order else None,
                "shipment_found": shipment is not None,
                "policy_allowed": policy.allowed if policy else None,
            },
        )
        if order is None:
            return {
                "order": None,
                "shipment": None,
                "policy": None,
                "phase": PHASE_BUSINESS_REJECTION,
                "reply_kind": "order_not_found",
            }
        return {"order": order, "shipment": shipment, "policy": policy}

    async def select_plan(self, state: AgentState) -> dict:
        ticket = state["ticket"]
        # Resume: a ticket already parked at the approval gate reuses the
        # approval-bound stored plan — a fresh LLM proposal must never
        # replace the plan a human reviewed.
        if ticket.status is TicketStatus.PENDING_APPROVAL:
            latest = self.approvals.latest_for(ticket.ticket_id)
            if latest is not None:
                stored = self.plans.load_verified(latest.plan_id)
                if stored is None:
                    return {
                        "phase": PHASE_FATAL,
                        "error": (
                            "approval references a plan that is not "
                            "registered or failed integrity verification"
                        ),
                    }
                await self._audit(
                    state,
                    "agent_plan_proposed",
                    "success",
                    {"reused": True, "plan_id": stored.plan_id},
                )
                return {
                    "draft_plan": stored.content,
                    "plan_id": stored.plan_id,
                    "plan_hash": stored.content_hash,
                }
        context = PlanContext(
            ticket=ticket,
            order=state.get("order"),
            shipment=state.get("shipment"),
            policy=state.get("policy"),
            trusted_issue_type=state["trusted_issue_type"],
            trusted_risk_level=state["trusted_risk_level"],
        )
        draft, retries = await self._propose_with_fallback(context)
        await self._audit(
            state,
            "agent_plan_proposed",
            "success",
            {
                "reused": False,
                "retries": retries,
                "proposed_action": draft.action,
            },
        )
        return {"draft_plan": draft}

    async def _propose_with_fallback(
        self, context: PlanContext
    ) -> tuple[ResolutionPlan, int]:
        last_error: Exception | None = None
        for attempt in range(_PLAN_PROPOSAL_ATTEMPTS):
            try:
                draft = await asyncio.wait_for(
                    self.planner.propose(context), timeout=self.llm_timeout
                )
                if draft.steps:
                    return draft, attempt
                last_error = ValueError("proposal has no steps")
            except Exception as exc:
                last_error = exc
                logger.exception("plan proposal attempt %d failed", attempt + 1)
        # Bounded retries exhausted: fall back to the deterministic
        # rule-based proposal. Never invent facts to keep the run alive.
        logger.error(
            "plan proposal failed after %d attempts (%s); using the default proposer",
            _PLAN_PROPOSAL_ATTEMPTS,
            last_error,
        )
        return await DefaultPlanProposer().propose(context), _PLAN_PROPOSAL_ATTEMPTS

    async def validate_plan(self, state: AgentState) -> dict:
        """Server-side validation and registration. The stored plan is the
        only version that may execute; LLM claims are re-derived from
        trusted records (issue type), or rejected outright when they
        contradict ticket/order/amount bindings."""
        ticket = state["ticket"]
        order = state.get("order")
        policy = state.get("policy")
        draft = state["draft_plan"]
        reused = state.get("plan_id") is not None
        now = self._clock()

        async def reject(reply_kind: str, reason: str) -> dict:
            await self._audit(
                state,
                "agent_plan_validated",
                "business_rejection",
                {"reply_kind": reply_kind, "reason": reason},
            )
            return {
                "phase": PHASE_BUSINESS_REJECTION,
                "reply_kind": reply_kind,
                "error": reason,
            }

        # Shared with RefundService (approval_requirement) so the service
        # boundary cannot drift from the graph: effective risk = max(ticket
        # risk, policy level); HIGH-or-above always requires approval.
        effective_risk, requires_approval = approval_requirement(
            state["trusted_risk_level"], policy
        )

        if draft.action not in ("refund", "note", "reply_only"):
            return await reject("invalid_plan", f"unsupported plan action {draft.action!r}")
        if draft.action == "refund":
            if policy is None or not policy.allowed:
                return await reject("policy_denied", "refund policy does not allow this refund")
            if order is None:
                return await reject("order_not_found", "cannot refund an order that does not exist")
            # Binding checks: the plan must be for the ticket's order, at
            # the ticket's claimed amount. Contradictions are rejected, not
            # silently corrected — except the issue type, which is advisory
            # and always re-derived from the ticket record.
            if draft.order_id != ticket.order_id:
                return await reject("plan_order_mismatch", "plan references an order not bound to the ticket")
            if ticket.claimed_amount is None:
                return await reject("no_claim_amount", "ticket carries no claim amount")
            if draft.amount is None:
                return await reject("invalid_plan", "refund plan must state an amount")
            if draft.amount != ticket.claimed_amount:
                return await reject("plan_amount_mismatch", "plan amount differs from the ticket claim")
            if draft.amount > order.total_amount:
                return await reject("claim_exceeds_total", "claim exceeds the order total")

        if reused:
            # The stored content is immutable and already hash-bound to the
            # approval; reusing it as-is keeps the content hash intact.
            plan = draft
        else:
            plan = ResolutionPlan(
                plan_id=f"PLAN-{uuid4().hex[:12]}",
                ticket_id=ticket.ticket_id,
                order_id=ticket.order_id if draft.action == "refund" else None,
                issue_type=ticket.issue_type,
                action=draft.action,
                amount=draft.amount if draft.action == "refund" else None,
                risk_level=effective_risk,
                requires_approval=requires_approval,
                steps=draft.steps,
                created_at=now,
            )
        try:
            stored = self.plans.register(plan, run_id=state["run_id"], created_at=now)
        except ValueError as exc:
            await self._audit(
                state,
                "agent_plan_validated",
                "execution_failure",
                {"reason": str(exc)},
            )
            return {"phase": PHASE_FATAL, "error": str(exc)}

        await self._audit(
            state,
            "agent_plan_validated",
            "success",
            {
                "plan_id": stored.plan_id,
                "plan_content_hash": stored.content_hash,
                "action": plan.action,
                "amount": str(plan.amount) if plan.amount is not None else None,
                "risk_level": plan.risk_level.value,
                "requires_approval": plan.requires_approval,
                "reused": reused,
                "draft_issue_mismatch": (
                    draft.issue_type is not None
                    and draft.issue_type is not ticket.issue_type
                ),
            },
        )

        gate = "none_required"
        approval = None
        if plan.requires_approval:
            latest = self.approvals.latest_for(ticket.ticket_id)
            approval = latest
            gate = self._evaluate_gate(latest, stored, now)
        phase, reply_kind, error = self._gate_phase(gate)
        await self._audit(
            state,
            "agent_approval_gate",
            "success",
            {"gate": gate},
        )
        return {
            "plan": plan,
            "plan_id": stored.plan_id,
            "plan_hash": stored.content_hash,
            "approval_gate": gate,
            "approval": approval,
            "phase": phase,
            "reply_kind": reply_kind,
            "error": error,
        }

    def _evaluate_gate(self, latest: Approval | None, stored, now: datetime) -> str:
        """Re-verify the approval against the registered plan at resume
        time: status, validity window, plan id, content hash and bound
        fields must all match."""
        if latest is None:
            return "missing"
        if latest.status is ApprovalStatus.PENDING:
            return "pending"
        if latest.status is ApprovalStatus.REJECTED:
            return "rejected"
        if latest.status is ApprovalStatus.EXPIRED:
            return "expired"
        if latest.expires_at is not None and now > latest.expires_at:
            return "expired"
        if latest.plan_id != stored.plan_id:
            return "mismatch"
        # A missing hash can never equal the stored plan's hash: fail closed.
        if latest.plan_content_hash != stored.content_hash:
            return "mismatch"
        plan = stored.content
        if (
            latest.ticket_id != plan.ticket_id
            or latest.order_id != plan.order_id
            or latest.action != plan.action
            or latest.amount != plan.amount
            or RISK_SEVERITY[latest.risk_level] != RISK_SEVERITY[plan.risk_level]
        ):
            return "mismatch"
        return "approved"

    def _gate_phase(self, gate: str) -> tuple[str, str | None, str | None]:
        if gate in ("none_required", "approved"):
            return PHASE_CONTINUE, None, None
        if gate in ("missing", "pending", "expired"):
            return PHASE_AWAIT_APPROVAL, None, None
        if gate == "rejected":
            return PHASE_BUSINESS_REJECTION, "approval_rejected", None
        # mismatch: approval does not bind the registered plan — fail closed.
        return PHASE_FATAL, None, "approval does not match the registered plan"

    async def request_approval(self, state: AgentState) -> dict:
        plan = state["plan"]
        stored = self.plans.load_verified(state["plan_id"])
        if stored is None:
            return {
                "phase": PHASE_FATAL,
                "error": "plan missing or failed integrity verification at approval request",
            }
        approval = await self.approvals.request(
            plan=stored,
            action=plan.action,
            justification=(
                f"High-risk {plan.action} requires a human decision."
            ),
            requested_at=self._clock(),
            ctx=self._ctx(state),
        )
        # The request is idempotent; a failed transition (ticket already
        # terminal, or another run moved it) changes nothing and never
        # pulls the ticket backwards.
        self.tools.tickets.transition_status(
            state["ticket_id"],
            TicketStatus.PENDING_APPROVAL,
            now=self._clock(),
            allowed_from=(TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        )
        return {
            "approval": approval,
            "phase": PHASE_PENDING_APPROVAL,
            "reply_kind": "pending_approval",
        }

    async def execute(self, state: AgentState) -> dict:
        if state["iterations"] >= state["max_iterations"]:
            return {
                "phase": PHASE_FATAL,
                "error": "iteration limit reached before execution",
                "reply_kind": "iteration_limit",
            }
        iterations = state["iterations"] + 1
        stored = self.plans.load_verified(state["plan_id"])
        if stored is None:
            return {
                "iterations": iterations,
                "phase": PHASE_FATAL,
                "error": "plan missing or failed integrity verification at execution",
            }
        plan = stored.content
        if plan.requires_approval and state["approval_gate"] != "approved":
            return {
                "iterations": iterations,
                "phase": PHASE_FATAL,
                "error": (
                    f"plan requires approval but the gate is "
                    f"{state['approval_gate']!r}"
                ),
            }
        ctx = self._ctx(state)
        summary: dict[str, Any] = {"action": plan.action}
        phase = PHASE_SUCCESS
        reply_kind = None
        refund_id = None
        if plan.action == "refund":
            key = f"agent-refund-{plan.ticket_id}-{plan.plan_id}"
            result = await self.tools.create_refund(
                ctx,
                order_id=plan.order_id,
                amount=plan.amount,
                issue_type=plan.issue_type,
                idempotency_key=key,
                plan_id=plan.plan_id,
            )
            summary.update(
                {
                    "tool": "create_refund",
                    "success": result.success,
                    "error_code": result.error_code.value if result.error_code else None,
                }
            )
            phase, reply_kind = self._execution_phase(result.error_code)
            if result.data is not None and hasattr(result.data, "refund_id"):
                refund_id = result.data.refund_id
                summary["refund_id"] = refund_id
        elif plan.action == "note":
            note_text = "; ".join(
                f"{step.action}: {step.reason}" for step in plan.steps
            )
            result = await self.tools.add_ticket_note(
                ctx, ticket_id=plan.ticket_id, note=note_text
            )
            summary.update({"tool": "add_ticket_note", "success": result.success})
            if not result.success:
                phase, reply_kind = PHASE_FATAL, None
        await self._audit(
            state,
            "agent_execute",
            "success" if phase in (PHASE_SUCCESS, PHASE_UNKNOWN) else phase,
            summary,
        )
        return {
            "iterations": iterations,
            "idempotency_key": (
                f"agent-refund-{plan.ticket_id}-{plan.plan_id}"
                if plan.action == "refund"
                else None
            ),
            "execution_summary": summary,
            "verified_facts": (
                {"order_id": plan.order_id, "refund_id": refund_id}
                if plan.action == "refund"
                else {}
            ),
            "phase": phase,
            "reply_kind": reply_kind,
        }

    def _execution_phase(self, code) -> tuple[str, str | None]:
        if code is None:
            return PHASE_SUCCESS, None
        if code is ToolErrorCode.UNKNOWN_OUTCOME:
            return PHASE_UNKNOWN, None
        if code in (
            ToolErrorCode.UPSTREAM_TIMEOUT,
            ToolErrorCode.CONCURRENT_PROCESSING,
        ):
            return PHASE_RETRY, None
        if code is ToolErrorCode.UNRESOLVED_REFUND:
            return PHASE_UNRESOLVED, "unresolved_refund"
        if code in (ToolErrorCode.APPROVAL_REQUIRED, ToolErrorCode.APPROVAL_EXPIRED):
            return PHASE_AWAIT_APPROVAL, None
        if code is ToolErrorCode.APPROVAL_REJECTED:
            return PHASE_BUSINESS_REJECTION, "approval_rejected"
        if code is ToolErrorCode.POLICY_DENIED:
            return PHASE_BUSINESS_REJECTION, "policy_denied"
        if code is ToolErrorCode.AMOUNT_MISMATCH:
            return PHASE_BUSINESS_REJECTION, "amount_mismatch"
        if code is ToolErrorCode.ALREADY_REFUNDED:
            return PHASE_BUSINESS_REJECTION, "already_refunded"
        if code is ToolErrorCode.CLAIM_EXCEEDED:
            return PHASE_BUSINESS_REJECTION, "claim_exceeded"
        if code is ToolErrorCode.GATEWAY_REJECTED:
            return PHASE_BUSINESS_REJECTION, "gateway_rejected"
        # APPROVAL_MISMATCH / IDEMPOTENCY_CONFLICT / ISSUE_TYPE_MISMATCH /
        # TICKET_ORDER_MISMATCH / INVALID_INPUT / INTERNAL_ERROR: a
        # validated plan hitting these means tampering or an internal bug.
        return PHASE_FATAL, None

    async def verify_result(self, state: AgentState) -> dict:
        """Re-read persisted state; never trust the ToolResult alone."""
        plan = state["plan"]
        ctx = self._ctx(state)
        if plan.action == "refund":
            record = self.tools.refund_store.get_by_key(state["idempotency_key"])
            if record is None:
                await self._audit(state, "agent_verify", "execution_failure", {})
                return {
                    "phase": PHASE_FATAL,
                    "error": "refund record missing after execution",
                }
            if (
                record.order_id != plan.order_id
                or record.amount != plan.amount
                or record.plan_id != plan.plan_id
                or record.ticket_id != plan.ticket_id
            ):
                await self._audit(
                    state,
                    "agent_verify",
                    "execution_failure",
                    {"reason": "record does not match the executed plan"},
                )
                return {
                    "phase": PHASE_FATAL,
                    "error": "refund record does not match the executed plan",
                }
            status = record.status
            await self._audit(
                state,
                "agent_verify",
                "success",
                {
                    "refund_id": record.refund_id,
                    "refund_status": status.value,
                },
            )
            if status is RefundStatus.PROCESSED:
                return {
                    "phase": PHASE_VERIFIED,
                    "reply_kind": "success_refund",
                    "refund_status": status,
                    "verified_facts": {
                        "amount": str(record.amount),
                        "currency": record.currency,
                        "order_id": record.order_id,
                        "refund_id": record.refund_id,
                    },
                }
            if status is RefundStatus.UNKNOWN:
                return {
                    "phase": PHASE_NEEDS_VERIFICATION,
                    "reply_kind": "needs_verification",
                    "refund_status": status,
                    "verified_facts": {
                        "order_id": record.order_id,
                        "refund_id": record.refund_id,
                    },
                }
            if status is RefundStatus.FAILED:
                if state["iterations"] < state["max_iterations"]:
                    return {"phase": PHASE_RETRY, "refund_status": status}
                return {
                    "phase": PHASE_FATAL,
                    "error": "iteration limit reached after transient failures",
                }
            # REJECTED (or an unexpected PENDING): refuse, never retry.
            return {
                "phase": PHASE_NEEDS_VERIFICATION if status is RefundStatus.PENDING else PHASE_BUSINESS_REJECTION,
                "reply_kind": (
                    "needs_verification"
                    if status is RefundStatus.PENDING
                    else "gateway_rejected"
                ),
                "refund_status": status,
                "verified_facts": {
                    "order_id": record.order_id,
                    "refund_id": record.refund_id,
                },
            }
        if plan.action == "note":
            notes = self.tools.notes.list_for(plan.ticket_id)
            note_text = "; ".join(
                f"{step.action}: {step.reason}" for step in plan.steps
            )
            if any(n.text == note_text for n in notes):
                await self._audit(state, "agent_verify", "success", {})
                return {
                    "phase": PHASE_VERIFIED,
                    "reply_kind": "note_added",
                    "verified_facts": {"note_added": True},
                }
            return {
                "phase": PHASE_FATAL,
                "error": "note record missing after execution",
            }
        # reply_only: the "result" is the reply itself, delivered next.
        return {
            "phase": PHASE_VERIFIED,
            "reply_kind": "reply_only",
            "verified_facts": {
                "order_id": state["ticket"].order_id,
                "order_status": (
                    state["order"].status.value if state.get("order") else None
                ),
            },
        }

    async def reply(self, state: AgentState) -> dict:
        facts = self._reply_facts(state)
        generated = ""
        try:
            generated = await asyncio.wait_for(
                self.reply_generator.generate(facts), timeout=self.llm_timeout
            )
        except Exception:
            logger.exception("reply generation failed; using the verified template")
        accepted = bool(generated) and verify_reply(generated, facts)
        final = generated if accepted else facts.template
        await self._audit(
            state,
            "agent_reply",
            "success" if accepted else "fallback",
            {"reply_kind": facts.kind, "used_template": not accepted},
        )
        return {"final_reply": final, "phase": "reply_done"}

    def _reply_facts(self, state: AgentState) -> ReplyFacts:
        kind = state.get("reply_kind") or "error"
        order_id = state["ticket"].order_id
        refund_id = state.get("verified_facts", {}).get("refund_id")
        amount = state.get("verified_facts", {}).get("amount")
        refused = (
            "refunded successfully",
            "has been processed",
            "processed successfully",
        )
        if kind == "success_refund":
            lines = (
                f"Refund of {amount} USD for order {order_id} has been processed.",
                f"Refund reference: {refund_id}.",
            )
            return ReplyFacts(
                kind=kind,
                lines=lines,
                required_tokens=("processed",),
                forbidden_tokens=("rejected", "not processed", "unknown"),
                template=" ".join(lines),
            )
        if kind == "needs_verification":
            lines = (
                f"Your refund for order {order_id} is being confirmed with the "
                "payment provider.",
                f"Reference: {refund_id}. We will follow up once confirmed.",
            )
            return ReplyFacts(
                kind=kind,
                lines=lines,
                required_tokens=("confirm",),
                forbidden_tokens=refused,
                template=" ".join(lines),
            )
        if kind == "unresolved_refund":
            lines = (
                f"A previous refund for order {order_id} is still being resolved "
                f"(reference {refund_id}).",
                "We cannot start another refund until it is settled.",
            )
            return ReplyFacts(
                kind=kind,
                lines=lines,
                required_tokens=("cannot",),
                forbidden_tokens=refused,
                template=" ".join(lines),
            )
        if kind == "reply_only":
            status = state.get("verified_facts", {}).get("order_status")
            lines = (f"Your order {order_id} is currently {status}.",)
            return ReplyFacts(
                kind=kind,
                lines=lines,
                required_tokens=(str(status),),
                forbidden_tokens=refused,
                template=" ".join(lines),
            )
        if kind == "note_added":
            lines = (
                "We recorded your request on the ticket and it will be "
                "followed up by our team.",
            )
            return ReplyFacts(
                kind=kind,
                lines=lines,
                forbidden_tokens=refused,
                template=" ".join(lines),
            )
        reason = {
            "order_not_found": f"order {order_id} not found",
            "policy_denied": "the refund policy does not allow this refund",
            "plan_amount_mismatch": "the requested amount does not match the ticket claim",
            "claim_exceeds_total": "the claim exceeds the order total",
            "plan_order_mismatch": "the request references an order not bound to this ticket",
            "already_refunded": "the order was already refunded in full",
            "claim_exceeded": "this ticket's claim has already been refunded",
            "gateway_rejected": "the payment provider rejected the refund",
            "approval_rejected": "human review rejected this request",
            "invalid_plan": "the request could not be validated",
            "ticket_not_found": "the ticket does not exist",
            "already_resolved": "this ticket was already resolved",
            "iteration_limit": "the automatic processing limit was reached",
        }.get(kind, "the request could not be completed")
        lines = (
            f"We could not process this request: {reason}.",
            "Your ticket remains open and will be reviewed by our team.",
        )
        return ReplyFacts(
            kind=kind,
            lines=lines,
            forbidden_tokens=refused,
            template=" ".join(lines),
        )

    async def finalize(self, state: AgentState) -> dict:
        now = self._clock()
        phase = state.get("phase", PHASE_FATAL)
        ticket = self.tools.tickets.get(state["ticket_id"])
        reply_kind = state.get("reply_kind")
        desired_status: TicketStatus | None = None
        if phase == PHASE_PENDING_APPROVAL:
            run_status = AgentRunStatus.AWAITING_APPROVAL
            # ticket already PENDING_APPROVAL (set by request_approval)
            error = None
        elif phase == PHASE_FATAL:
            run_status = AgentRunStatus.FAILED
            error = state.get("error") or "run failed"
            desired_status = TicketStatus.IN_PROGRESS
        elif reply_kind in ("success_refund", "reply_only", "note_added"):
            run_status = AgentRunStatus.COMPLETED
            error = None
            # Business completion conditions are met and verified: only now
            # may the ticket be resolved.
            desired_status = TicketStatus.RESOLVED
        else:
            # Refusals, needs-verification and unresolved states: the run
            # is complete, the ticket is NOT resolved.
            run_status = AgentRunStatus.COMPLETED
            error = None
            if ticket is not None and ticket.status in (
                TicketStatus.PENDING_APPROVAL,
                TicketStatus.OPEN,
            ):
                desired_status = TicketStatus.IN_PROGRESS
        if desired_status is not None and ticket is not None:
            # Atomic conditional transition: the move only happens when the
            # ticket still sits in an allowed, non-terminal state. A
            # concurrent run that already resolved (or closed) the ticket
            # is therefore never pulled backwards by a later run that
            # failed — the guard is checked and applied under one lock.
            self.tools.tickets.transition_status(
                state["ticket_id"],
                desired_status,
                now=now,
                allowed_from=(
                    TicketStatus.OPEN,
                    TicketStatus.IN_PROGRESS,
                    TicketStatus.PENDING_APPROVAL,
                ),
            )
            ticket = self.tools.tickets.get(state["ticket_id"])
        updated = self.runs.update(
            state["run_id"],
            status=run_status,
            iterations=state.get("iterations", 0),
            error=error,
            finished_at=now,
        )
        await self._audit(
            state,
            "agent_run_finished",
            "success" if run_status is not AgentRunStatus.FAILED else "execution_failure",
            {
                "phase": phase,
                "run_status": run_status.value,
                # Audit the ACTUAL final status: the conditional transition
                # may have been refused because another run already moved
                # the ticket to a terminal state.
                "ticket_status": (
                    ticket.status.value if ticket is not None else None
                ),
                "refund_status": (
                    state.get("refund_status").value
                    if state.get("refund_status")
                    else None
                ),
                "reply_kind": reply_kind,
                "iterations": state.get("iterations", 0),
            },
        )
        final_phase = {
            AgentRunStatus.AWAITING_APPROVAL: PHASE_PENDING_APPROVAL,
            AgentRunStatus.FAILED: "failed",
            AgentRunStatus.COMPLETED: "completed",
        }.get(run_status, "completed")
        return {"run_status": run_status, "phase": final_phase, "error": error}


# --- routing ---


def _route_continue(state: AgentState) -> str:
    phase = state.get("phase", "continue")
    return phase


_ROUTES_INIT = {
    "continue": "classify",
    PHASE_BUSINESS_REJECTION: "reply",
    PHASE_FATAL: "finalize",
}
_ROUTES_GATHER = {
    "continue": "select_plan",
    PHASE_BUSINESS_REJECTION: "reply",
    PHASE_FATAL: "finalize",
}
_ROUTES_SELECT = {
    "continue": "validate_plan",
    PHASE_FATAL: "finalize",
}
_ROUTES_VALIDATE = {
    "continue": "execute",
    PHASE_AWAIT_APPROVAL: "request_approval",
    PHASE_BUSINESS_REJECTION: "reply",
    PHASE_FATAL: "finalize",
}
_ROUTES_EXECUTE = {
    PHASE_SUCCESS: "verify_result",
    PHASE_UNKNOWN: "verify_result",
    PHASE_RETRY: "execute",
    PHASE_BUSINESS_REJECTION: "reply",
    PHASE_AWAIT_APPROVAL: "request_approval",
    PHASE_UNRESOLVED: "reply",
    PHASE_FATAL: "finalize",
}
_ROUTES_VERIFY = {
    PHASE_VERIFIED: "reply",
    PHASE_NEEDS_VERIFICATION: "reply",
    PHASE_RETRY: "execute",
    PHASE_BUSINESS_REJECTION: "reply",
    PHASE_FATAL: "finalize",
}


def build_agent_graph(agent: SupportFlowAgent):
    graph = StateGraph(AgentState)
    graph.add_node("init_run", agent.init_run)
    graph.add_node("classify", agent.classify)
    graph.add_node("gather_context", agent.gather_context)
    graph.add_node("select_plan", agent.select_plan)
    graph.add_node("validate_plan", agent.validate_plan)
    graph.add_node("request_approval", agent.request_approval)
    graph.add_node("execute", agent.execute)
    graph.add_node("verify_result", agent.verify_result)
    graph.add_node("reply", agent.reply)
    graph.add_node("finalize", agent.finalize)

    graph.add_edge(START, "init_run")
    graph.add_conditional_edges("init_run", _route_continue, _ROUTES_INIT)
    graph.add_edge("classify", "gather_context")
    graph.add_conditional_edges(
        "gather_context", _route_continue, _ROUTES_GATHER
    )
    graph.add_conditional_edges(
        "select_plan", _route_continue, _ROUTES_SELECT
    )
    graph.add_conditional_edges(
        "validate_plan", _route_continue, _ROUTES_VALIDATE
    )
    graph.add_edge("request_approval", "finalize")
    graph.add_conditional_edges("execute", _route_continue, _ROUTES_EXECUTE)
    graph.add_conditional_edges(
        "verify_result", _route_continue, _ROUTES_VERIFY
    )
    graph.add_edge("reply", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
