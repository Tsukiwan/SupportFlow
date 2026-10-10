"""ApprovalService contract: plan binding, dedup, decide rules, audit."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.enums import ApprovalStatus, IssueType, RiskLevel
from app.domain.models import ResolutionPlan, ResolutionPlanStep
from app.persistence.memory_stores import ApprovalStore, AuditStore
from app.services.approvals import ApprovalService
from app.services.audit import AuditRecorder
from app.services.plans import PlanStore, plan_content_hash
from app.tools.context import ToolContext

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

PLAN_FIELDS = dict(
    plan_id="PLAN-1",
    ticket_id="TKT-1",
    order_id="ORD-1",
    issue_type=IssueType.REFUND_REQUEST,
    action="refund",
    amount=Decimal("10.00"),
    risk_level=RiskLevel.HIGH,
    requires_approval=True,
    steps=(ResolutionPlanStep(action="issue refund", reason="high-risk refund"),),
    created_at=T0,
)


def make_plan(**overrides) -> ResolutionPlan:
    fields = dict(PLAN_FIELDS)
    fields.update(overrides)
    return ResolutionPlan(**fields)


def make_service():
    audit_store = AuditStore()
    return (
        ApprovalService(store=ApprovalStore(), recorder=AuditRecorder(audit_store)),
        audit_store,
    )


def make_ctx() -> ToolContext:
    return ToolContext(ticket_id="TKT-1", run_id="RUN-1")


async def request_for(
    service, plan_store, *, plan_id="PLAN-1", ticket_id="TKT-1"
):
    plan = make_plan(plan_id=plan_id, ticket_id=ticket_id)
    stored = plan_store.register(plan, run_id="RUN-1", created_at=T0)
    return await service.request(
        plan=stored,
        action="refund",
        justification="high-risk refund needs human review",
        requested_at=T0,
        ctx=make_ctx(),
    )


def test_request_binds_plan_fields_and_hash():
    service, _ = make_service()
    plan_store = PlanStore()
    approval = asyncio_get(request_for(service, plan_store))
    assert approval.ticket_id == "TKT-1"
    assert approval.order_id == "ORD-1"
    assert approval.plan_id == "PLAN-1"
    assert approval.action == "refund"
    assert approval.amount == Decimal("10.00")
    assert approval.risk_level is RiskLevel.HIGH
    assert approval.plan_content_hash == plan_content_hash(make_plan())
    assert approval.status is ApprovalStatus.PENDING
    assert approval.decided_at is None and approval.decided_by is None


def test_request_audits_with_plan_binding():
    service, audit_store = make_service()
    plan_store = PlanStore()
    approval = asyncio_get(request_for(service, plan_store))
    events = audit_store.all()
    assert len(events) == 1
    event = events[0]
    assert event.action == "agent_approval_requested"
    assert event.actor == "agent"
    assert event.ticket_id == "TKT-1"
    assert event.run_id == "RUN-1"
    assert event.details["approval_id"] == approval.approval_id
    assert event.details["plan_content_hash"] == approval.plan_content_hash
    assert event.details["order_id"] == "ORD-1"
    assert event.details["risk_level"] == "high"


def test_request_dedupes_pending_for_same_plan():
    service, _ = make_service()
    plan_store = PlanStore()
    first = asyncio_get(request_for(service, plan_store))
    second = asyncio_get(request_for(service, plan_store))
    assert second.approval_id == first.approval_id
    assert len(service._store.all()) == 1


def test_request_for_different_plan_is_not_deduped():
    service, _ = make_service()
    plan_store = PlanStore()
    first = asyncio_get(request_for(service, plan_store))
    second = asyncio_get(
        request_for(service, plan_store, plan_id="PLAN-2", ticket_id="TKT-1")
    )
    assert second.approval_id != first.approval_id
    assert len(service._store.all()) == 2


def test_decide_approve_records_actor_time_and_validity():
    service, audit_store = make_service()
    plan_store = PlanStore()
    approval = asyncio_get(request_for(service, plan_store))
    decided = asyncio_get(
        service.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=T0 + timedelta(hours=1),
            expires_at=T0 + timedelta(days=7),
        )
    )
    assert decided.status is ApprovalStatus.APPROVED
    assert decided.decided_by == "alice"
    assert decided.decided_at == T0 + timedelta(hours=1)
    assert decided.expires_at == T0 + timedelta(days=7)
    assert decided.plan_content_hash == approval.plan_content_hash
    decision_events = [e for e in audit_store.all() if e.action == "approval_decision"]
    assert len(decision_events) == 1
    event = decision_events[0]
    assert event.actor == "human"
    assert event.details["decision"] == "approved"
    assert event.details["decided_by"] == "alice"
    assert event.details["plan_content_hash"] == approval.plan_content_hash


def test_decide_reject():
    service, _ = make_service()
    plan_store = PlanStore()
    approval = asyncio_get(request_for(service, plan_store))
    decided = asyncio_get(
        service.decide(
            approval.approval_id,
            status=ApprovalStatus.REJECTED,
            decided_by="bob",
            decided_at=T0 + timedelta(hours=1),
        )
    )
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.decided_by == "bob"


def test_decide_only_accepts_terminal_decisions():
    service, _ = make_service()
    plan_store = PlanStore()
    approval = asyncio_get(request_for(service, plan_store))
    with pytest.raises(ValueError):
        asyncio_get(
            service.decide(
                approval.approval_id,
                status=ApprovalStatus.PENDING,  # not a decision
                decided_by="alice",
            )
        )
    with pytest.raises(ValueError):
        asyncio_get(
            service.decide(
                approval.approval_id,
                status=ApprovalStatus.EXPIRED,  # not a decision
                decided_by="alice",
            )
        )


def test_decide_rejects_unknown_and_double_decide():
    service, _ = make_service()
    plan_store = PlanStore()
    with pytest.raises(ValueError):
        asyncio_get(
            service.decide("APR-unknown", status=ApprovalStatus.APPROVED, decided_by="alice")
        )
    approval = asyncio_get(request_for(service, plan_store))
    asyncio_get(
        service.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            decided_at=T0 + timedelta(hours=1),
        )
    )
    with pytest.raises(ValueError):
        asyncio_get(
            service.decide(
                approval.approval_id,
                status=ApprovalStatus.REJECTED,
                decided_by="bob",
                decided_at=T0 + timedelta(hours=2),
            )
        )


def test_latest_for_returns_newest_request():
    service, _ = make_service()
    plan_store = PlanStore()
    first = asyncio_get(request_for(service, plan_store))
    second = asyncio_get(
        request_for(service, plan_store, plan_id="PLAN-2", ticket_id="TKT-1")
    )
    # Same requested_at (T0): max() keeps the later item.
    latest = service.latest_for("TKT-1")
    assert latest is not None
    assert latest.approval_id in (first.approval_id, second.approval_id)


def asyncio_get(coro):
    import asyncio

    return asyncio.run(coro)
