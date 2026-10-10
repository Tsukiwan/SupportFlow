"""Agent graph state and routing phases.

The state carries only *server-derived* facts (ticket/order/policy from
stores, the registered plan id) and routing metadata. LLM outputs are
carried as clearly-labelled proposals that nodes re-verify before acting
on them; a proposal is never authoritative state.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from app.domain.enums import AgentRunStatus, IssueType, RefundStatus, RiskLevel
from app.domain.models import Approval, Order, ResolutionPlan, Shipment, Ticket
from app.services.refund_policy import RefundPolicy


class AgentState(TypedDict, total=False):
    # Identifiers and guardrails.
    ticket_id: str
    run_id: str
    max_iterations: int
    iterations: int
    started_at: datetime

    # Trusted facts gathered from stores.
    ticket: Ticket | None
    order: Order | None
    shipment: Shipment | None
    policy: RefundPolicy | None
    trusted_issue_type: IssueType | None
    trusted_risk_level: RiskLevel | None

    # LLM proposals (advisory; validated before use).
    classification_proposal: dict
    draft_plan: ResolutionPlan | None

    # The registered, server-validated plan that may execute.
    plan: ResolutionPlan | None
    plan_id: str | None
    plan_hash: str | None

    # Approval gate: none_required / approved / missing / pending /
    # rejected / expired / mismatch.
    approval_gate: str
    approval: Approval | None

    # Execution bookkeeping.
    idempotency_key: str | None
    execution_summary: dict
    verified_facts: dict
    reply_kind: str

    # Routing and outcome.
    phase: str
    error: str | None
    final_reply: str | None
    run_status: AgentRunStatus | None
    refund_status: RefundStatus | None


# Routing phases (values used by conditional edges).
PHASE_CONTINUE = "continue"
PHASE_AWAIT_APPROVAL = "await_approval"
PHASE_BUSINESS_REJECTION = "business_rejection"
PHASE_FATAL = "fatal"
PHASE_RETRY = "retry"
PHASE_SUCCESS = "success"
PHASE_UNKNOWN = "unknown"
PHASE_VERIFIED = "verified"
PHASE_NEEDS_VERIFICATION = "needs_verification"
PHASE_UNRESOLVED = "unresolved"
PHASE_PENDING_APPROVAL = "pending_approval"
