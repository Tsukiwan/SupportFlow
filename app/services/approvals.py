"""Human-approval service: the only production entry to approval decisions.

The agent graph depends on the read-only :class:`ApprovalQueries` view and
can request approvals — it can never decide one. Deciding an approval is
``ApprovalService.decide``, called by the human side of the system (an
operator UI / API layer, outside the graph). Test fixtures that pre-seed
APPROVED records bypass this service on purpose and are test-only: the
production path to ``APPROVED`` / ``REJECTED`` goes through ``decide``.

Every decision records the actor, the decision time, an optional validity
window (``expires_at``) and — via the plan binding — the exact plan
content hash the human reviewed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from app.domain.enums import ApprovalStatus
from app.domain.models import Approval
from app.persistence.memory_stores import ApprovalStore
from app.services.audit import AuditRecorder
from app.services.plans import StoredPlan

logger = logging.getLogger(__name__)


def latest_approval(candidates: Sequence[Approval]) -> Approval | None:
    """Deterministic "latest approval" rule, shared by ApprovalService and
    RefundService so the gate and the service boundary always pick the
    same record: the highest ``requested_at`` wins; on an exact tie the
    later-inserted record wins (store order). A fixed test clock or two
    requests in the same instant therefore still yield one well-defined
    answer instead of depending on dict ordering."""
    if not candidates:
        return None
    latest = candidates[0]
    for candidate in candidates[1:]:
        if candidate.requested_at >= latest.requested_at:
            latest = candidate
    return latest


class ApprovalQueries(Protocol):
    """Read-only view of approvals exposed to the agent graph."""

    def latest_for(self, ticket_id: str) -> Approval | None: ...

    def get(self, approval_id: str) -> Approval | None: ...


class ApprovalRequestPort(Protocol):
    """The one write the agent may perform: create a PENDING request."""

    async def request(
        self,
        *,
        plan: StoredPlan,
        action: str,
        justification: str,
        requested_at: datetime | None = None,
        ctx=None,
    ) -> Approval: ...


class ApprovalAgentPort(ApprovalQueries, ApprovalRequestPort, Protocol):
    """Everything the agent graph may touch. ``decide`` is deliberately
    absent — decisions are made by the human-facing service, not the
    graph."""

    pass


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        *,
        recorder: AuditRecorder | None = None,
    ) -> None:
        self._store = store
        self._recorder = recorder

    # --- read-only queries (safe to hand to the agent) ---

    def latest_for(self, ticket_id: str) -> Approval | None:
        candidates = [a for a in self._store.all() if a.ticket_id == ticket_id]
        return latest_approval(candidates)

    def get(self, approval_id: str) -> Approval | None:
        return self._store.get(approval_id)

    # --- request (agent-facing) ---

    async def request(
        self,
        *,
        plan: StoredPlan,
        action: str,
        justification: str,
        requested_at: datetime | None = None,
        ctx=None,
    ) -> Approval:
        """Create a PENDING approval bound to a registered plan. Idempotent
        per (ticket, plan): an existing PENDING request for the same plan
        is returned instead of duplicated."""
        for existing in self._store.all():
            if (
                existing.ticket_id == plan.ticket_id
                and existing.plan_id == plan.plan_id
                and existing.status is ApprovalStatus.PENDING
            ):
                return existing
        approval = Approval(
            approval_id=f"APR-{uuid4().hex[:12]}",
            ticket_id=plan.content.ticket_id,
            order_id=plan.content.order_id or plan.ticket_id,
            plan_id=plan.plan_id,
            action=action,
            amount=plan.content.amount,
            risk_level=plan.content.risk_level,
            plan_content_hash=plan.content_hash,
            justification=justification,
            status=ApprovalStatus.PENDING,
            requested_at=requested_at or datetime.now(timezone.utc),
        )
        self._store.add(approval)
        if self._recorder is not None:
            await self._recorder.record(
                tool="agent_approval_requested",
                ctx=ctx,
                outcome="success",
                details={
                    "approval_id": approval.approval_id,
                    "plan_id": plan.plan_id,
                    "plan_content_hash": plan.content_hash,
                    "order_id": approval.order_id,
                    "risk_level": approval.risk_level.value,
                },
            )
        return approval

    # --- decide (human-facing; NOT reachable from the agent graph) ---

    async def decide(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> Approval:
        """Record a human decision. Only a PENDING approval can be decided,
        and only into APPROVED or REJECTED — the agent has no tool for this
        and cannot fabricate a decision."""
        if status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError("a decision must be APPROVED or REJECTED")
        approval = self._store.get(approval_id)
        if approval is None:
            raise ValueError(f"unknown approval {approval_id}")
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError(
                f"approval {approval_id} is {approval.status.value}, not pending"
            )
        decided = approval.model_copy(
            update={
                "status": status,
                "decided_at": decided_at or datetime.now(timezone.utc),
                "decided_by": decided_by,
                "expires_at": expires_at,
            }
        )
        self._store.add(decided)
        logger.info(
            "approval %s decided as %s by %s (plan %s)",
            approval_id,
            status.value,
            decided_by,
            approval.plan_id,
        )
        if self._recorder is not None:
            await self._recorder.record(
                tool="approval_decision",
                actor="human",
                outcome="success",
                details={
                    "approval_id": approval_id,
                    "decision": status.value,
                    "decided_by": decided_by,
                    "plan_id": approval.plan_id,
                    "plan_content_hash": approval.plan_content_hash,
                    "order_id": approval.order_id,
                },
            )
        return decided
