"""PlanStore: immutability, content hashing and drift detection."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain.enums import IssueType, RiskLevel
from app.domain.models import ResolutionPlan, ResolutionPlanStep
from app.services.plans import PlanStore, StoredPlan, plan_content_hash

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def make_plan(**overrides) -> ResolutionPlan:
    fields = dict(
        plan_id="PLAN-1",
        ticket_id="TKT-1",
        order_id="ORD-1",
        issue_type=IssueType.REFUND_REQUEST,
        action="refund",
        amount=Decimal("10.00"),
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        steps=(ResolutionPlanStep(action="issue refund", reason="within policy"),),
        created_at=T0,
    )
    fields.update(overrides)
    return ResolutionPlan(**fields)


def test_hash_is_deterministic():
    assert plan_content_hash(make_plan()) == plan_content_hash(make_plan())


def test_hash_changes_when_content_changes():
    assert plan_content_hash(make_plan()) != plan_content_hash(
        make_plan(amount=Decimal("11.00"))
    )


def test_hash_binds_plan_id():
    assert plan_content_hash(make_plan()) != plan_content_hash(
        make_plan(plan_id="PLAN-2")
    )


def test_register_and_load_verified():
    store = PlanStore()
    stored = store.register(make_plan(), run_id="RUN-1", created_at=T0)
    assert stored.content_hash == plan_content_hash(make_plan())
    loaded = store.load_verified("PLAN-1")
    assert loaded is not None
    assert loaded.content.model_dump() == stored.content.model_dump()
    assert store.verify("PLAN-1") == "ok"


def test_reregister_same_content_is_idempotent():
    store = PlanStore()
    first = store.register(make_plan(), run_id="RUN-1", created_at=T0)
    second = store.register(make_plan(), run_id="RUN-2", created_at=T0)
    assert second.content_hash == first.content_hash
    assert len(store.all()) == 1


def test_reregister_different_content_fails_closed():
    """A reused plan_id with modified content (e.g. a changed amount) must
    not shadow the plan an approval was granted for."""
    store = PlanStore()
    store.register(make_plan(), run_id="RUN-1", created_at=T0)
    with pytest.raises(ValueError):
        store.register(make_plan(amount=Decimal("999.00")), run_id="RUN-2", created_at=T0)
    # The original plan is untouched.
    loaded = store.load_verified("PLAN-1")
    assert loaded is not None
    assert loaded.content.amount == Decimal("10.00")


def test_load_verified_detects_drift():
    """White-box: if a stored record's hash no longer matches its content,
    load_verified must refuse to return the plan."""
    store = PlanStore()
    stored = store.register(make_plan(), run_id="RUN-1", created_at=T0)
    store._by_id["PLAN-1"] = StoredPlan(
        plan_id=stored.plan_id,
        ticket_id=stored.ticket_id,
        content=make_plan(amount=Decimal("50.00")),
        content_hash=stored.content_hash,  # stale hash over old content
        run_id="RUN-1",
        created_at=T0,
    )
    assert store.load_verified("PLAN-1") is None
    assert store.verify("PLAN-1") == "drift"
    assert store.verify("PLAN-missing") == "missing"


def test_missing_plan_is_missing():
    store = PlanStore()
    assert store.get("PLAN-9") is None
    assert store.load_verified("PLAN-9") is None
    assert store.verify("PLAN-9") == "missing"
