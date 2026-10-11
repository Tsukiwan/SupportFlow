"""Day 5 B/D: the agent refund chain over the real PostgreSQL wiring.

These tests run the *production* composition root
(``build_agent_wiring`` with ``store_backend=postgres``) against the
dedicated test database (guarded by ``SUPPORTFLOW_TEST_DATABASE_URL``
and the ``_test`` suffix rule in ``conftest``):

- B1: full agent flows — automatic refund, the approval two-phase flow,
  claim-cap authority, and a rebuild proving state survives re-wiring;
- B2: exception outcomes through the wired tools — UNKNOWN keeps the
  reservation and resolves by status query, transient failure releases
  and retries with the same key, REJECTED releases;
- D: safety regressions — concurrent creators, audit/refund transaction
  atomicity, and the audit↔refund linkage invariant.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.agent.llm import (
    DefaultClassifier,
    DefaultPlanProposer,
    DefaultReplyGenerator,
)
from app.agent.runner import build_agent_wiring, run_ticket
from app.agent.state import PHASE_PENDING_APPROVAL
from app.config import Settings
from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    RefundStatus,
)
from app.domain.models import AgentRun
from app.persistence.models import (
    AgentRunRow,
    AuditEventRow,
    OrderRow,
    PlanRow,
    RefundRow,
    TicketRow,
)
from app.persistence.pg_catalog import ensure_catalog_seeded
from app.persistence.pg_refund_store import PgRefundStore
from app.services.payments import GatewayOutcome, SimulatedPaymentGateway
from app.services.plans import plan_content_hash
from app.tools.context import ToolContext

PG_ORDER_ID = "ORD-1001"  # refundable: total 199.99, claimed 199.99
PG_TICKET_ID = "TKT-2001"  # low-risk, auto-approved path
APPROVAL_ORDER_ID = "ORD-1005"  # delivered_not_received: needs approval
APPROVAL_TICKET_ID = "TKT-2005"


def _pg_deps(monkeypatch, pg_url: str, gateway=None):
    """The production postgres wiring over the test database. Seeding is
    idempotent, so repeated wiring never overwrites persisted state."""
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_URL", pg_url)
    monkeypatch.setenv("SUPPORTFLOW_STORE_BACKEND", "postgres")
    settings = Settings(_env_file=None)
    return build_agent_wiring(
        settings=settings,
        gateway=gateway,
        classifier=DefaultClassifier(),
        planner=DefaultPlanProposer(),
        reply_generator=DefaultReplyGenerator(),
    )


def _session(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False)()


def _make_run(deps, ticket_id: str, run_id: str) -> None:
    deps.runs.create(
        AgentRun(
            run_id=run_id,
            ticket_id=ticket_id,
            status=AgentRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
    )


# ---------------------------------------------------------------------
# B1: full agent flows over the real wiring
# ---------------------------------------------------------------------


def test_automatic_refund_flow_persists_to_pg(pg_engine, pg_url, monkeypatch):
    deps = _pg_deps(monkeypatch, pg_url)
    outcome = asyncio.run(run_ticket(PG_TICKET_ID, deps))

    # The graph's final phase is "completed" for any finished run; the
    # proof of success is the persisted state below, not the phase label.
    assert outcome.phase == "completed"
    assert outcome.run_status == AgentRunStatus.COMPLETED
    assert outcome.plan_id is not None

    # The refund row, the plan row and the order accounting all live in
    # PostgreSQL — read them in a fresh, independent session.
    with _session(pg_engine) as s:
        refund = s.execute(select(RefundRow)).scalar_one()
        assert refund.status == "processed"
        assert refund.amount == Decimal("199.99")
        assert refund.order_id == PG_ORDER_ID
        assert refund.ticket_id == PG_TICKET_ID
        assert refund.plan_id == outcome.plan_id
        assert refund.idempotency_key == f"agent-refund-{PG_TICKET_ID}-{outcome.plan_id}"

        order = s.get(OrderRow, PG_ORDER_ID)
        assert order.refunded_amount == Decimal("199.99")
        assert order.reserved_amount == Decimal("0")

        plan = s.get(PlanRow, outcome.plan_id)
        assert plan is not None
        from app.domain.models import ResolutionPlan

        content = ResolutionPlan.model_validate(plan.content)
        assert plan.content_hash == plan_content_hash(content)

        run = s.get(AgentRunRow, outcome.run_id)
        assert run is not None
        assert run.status == "completed"

    # The graph re-reads persisted state through the wired PG store
    # (verify_result -> refund_store.get_by_key) — and the read-through
    # order store shows the settled balance, not a stale in-memory one.
    assert deps.tools.orders.get(PG_ORDER_ID).refunded_amount == Decimal("199.99")
    assert deps.tools.refund_store.get_by_key(
        f"agent-refund-{PG_TICKET_ID}-{outcome.plan_id}"
    ) is not None

    # Audit trail: run start, tool call and run finish all persisted.
    with _session(pg_engine) as s:
        actions = set(
            s.execute(
                select(AuditEventRow.action).where(
                    AuditEventRow.run_id == outcome.run_id
                )
            ).scalars()
        )
    assert "agent_run_started" in actions
    assert "create_refund" in actions
    assert "agent_run_finished" in actions


def test_rebuild_reads_persisted_state_and_second_run_refuses(
    pg_engine, pg_url, monkeypatch
):
    first_deps = _pg_deps(monkeypatch, pg_url)
    first = asyncio.run(run_ticket(PG_TICKET_ID, first_deps))
    assert first.phase == "completed"

    # Rebuild the entire application wiring (new engine, new stores) and
    # prove the state is still there — no in-memory state is reused.
    second_deps = _pg_deps(monkeypatch, pg_url)
    order = second_deps.tools.orders.get(PG_ORDER_ID)
    assert order.refunded_amount == Decimal("199.99")
    assert second_deps.tools.refund_store.count() == 1

    # A fresh run proposes a new plan (new idempotency key), and the
    # settled balance refuses it: no second refund, no double accounting.
    second = asyncio.run(run_ticket(PG_TICKET_ID, second_deps))
    assert second.phase == "completed"
    assert second.run_status == AgentRunStatus.COMPLETED
    assert second.final_reply is not None
    assert "We could not process this request" in second.final_reply
    with _session(pg_engine) as s:
        refunds = s.execute(select(RefundRow)).scalars().all()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert len(refunds) == 1
    assert order.refunded_amount == Decimal("199.99")
    assert order.reserved_amount == Decimal("0")


def test_approval_two_phase_flow_persists_plan_and_refund(
    pg_engine, pg_url, monkeypatch
):
    deps = _pg_deps(monkeypatch, pg_url)

    # Phase 1: the high-risk plan pauses at the human gate — nothing is
    # refunded, the plan is already registered (refunds.plan_id is a real
    # foreign key, so the registry write must precede any execution).
    first = asyncio.run(run_ticket(APPROVAL_TICKET_ID, deps))
    assert first.phase == PHASE_PENDING_APPROVAL
    assert first.run_status == AgentRunStatus.AWAITING_APPROVAL
    assert deps.tools.refund_store.count() == 0

    approval = deps.approvals.latest_for(APPROVAL_TICKET_ID)
    assert approval is not None
    asyncio.run(
        deps.approvals.decide(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="alice",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
    )

    # Phase 2: the resumed run re-verifies the approval (status, window,
    # plan content hash) against the PG-backed plan registry, then
    # executes through the same wired PG refund store.
    resumed = asyncio.run(run_ticket(APPROVAL_TICKET_ID, deps))
    assert resumed.phase == "completed"
    assert resumed.plan_id == approval.plan_id  # plan reused, not re-proposed

    with _session(pg_engine) as s:
        refund = s.execute(select(RefundRow)).scalar_one()
        assert refund.status == "processed"
        assert refund.plan_id == approval.plan_id
        plan = s.get(PlanRow, approval.plan_id)
        assert plan is not None
        assert plan.content_hash == approval.plan_content_hash
        order = s.get(OrderRow, APPROVAL_ORDER_ID)
        assert order.reserved_amount == Decimal("0")
        assert order.refunded_amount == refund.amount


def test_claim_cap_is_read_from_the_locked_ticket_row(
    pg_engine, pg_url, monkeypatch
):
    """The caller's claimed amount is never trusted: the store computes
    the claim cap from the locked ticket row. The in-memory ticket still
    claims 199.99; the database row is changed to 100.00, and a 150.00
    refund must be refused."""
    deps = _pg_deps(monkeypatch, pg_url)
    _make_run(deps, PG_TICKET_ID, "RUN-CLAIM-TEST")
    with pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE tickets SET claimed_amount = 100.00 "
                 "WHERE ticket_id = :tid"),
            {"tid": PG_TICKET_ID},
        )

    result = asyncio.run(
        deps.tools.create_refund(
            ToolContext(ticket_id=PG_TICKET_ID, run_id="RUN-CLAIM-TEST"),
            order_id=PG_ORDER_ID,
            amount=Decimal("150.00"),
            issue_type=IssueType.REFUND_REQUEST,
            idempotency_key="claim-cap-key",
        )
    )
    assert result.success is False
    assert result.error_code.value == "CLAIM_EXCEEDED"

    with _session(pg_engine) as s:
        assert s.execute(select(RefundRow)).scalars().all() == []
        order = s.get(OrderRow, PG_ORDER_ID)
    assert order.reserved_amount == Decimal("0")  # refused before reserving


# ---------------------------------------------------------------------
# B2: exception outcomes through the wired tools
# ---------------------------------------------------------------------


def test_unknown_outcome_keeps_reservation_and_resolves_by_query(
    pg_engine, pg_url, monkeypatch
):
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.UNKNOWN])
    deps = _pg_deps(monkeypatch, pg_url, gateway=gateway)
    _make_run(deps, PG_TICKET_ID, "RUN-UNKNOWN")
    ctx = ToolContext(ticket_id=PG_TICKET_ID, run_id="RUN-UNKNOWN")

    first = asyncio.run(
        deps.tools.create_refund(
            ctx, order_id=PG_ORDER_ID, amount=Decimal("60.00"),
            issue_type=IssueType.REFUND_REQUEST, idempotency_key="unknown-key",
        )
    )
    assert first.success is False
    assert first.error_code.value == "UNKNOWN_OUTCOME"
    with _session(pg_engine) as s:
        row = s.execute(select(RefundRow)).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert row.status == "unknown"
    assert order.reserved_amount == Decimal("60.00")  # fail-closed: kept
    assert order.refunded_amount == Decimal("0")

    # Same key: the original operation is queried, never re-executed.
    second = asyncio.run(
        deps.tools.create_refund(
            ctx, order_id=PG_ORDER_ID, amount=Decimal("60.00"),
            issue_type=IssueType.REFUND_REQUEST, idempotency_key="unknown-key",
        )
    )
    assert second.success is True
    assert gateway.execute_calls == 1  # the refund was never executed twice
    assert gateway.status_calls == 1
    with _session(pg_engine) as s:
        row = s.execute(select(RefundRow)).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert row.status == "processed"
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("60.00")


def test_transient_failure_releases_and_same_key_retry_succeeds(
    pg_engine, pg_url, monkeypatch
):
    gateway = SimulatedPaymentGateway(
        outcomes=[GatewayOutcome.TRANSIENT_FAILURE, GatewayOutcome.PROCESSED]
    )
    deps = _pg_deps(monkeypatch, pg_url, gateway=gateway)
    _make_run(deps, PG_TICKET_ID, "RUN-RETRY")
    ctx = ToolContext(ticket_id=PG_TICKET_ID, run_id="RUN-RETRY")

    first = asyncio.run(
        deps.tools.create_refund(
            ctx, order_id=PG_ORDER_ID, amount=Decimal("30.00"),
            issue_type=IssueType.REFUND_REQUEST, idempotency_key="retry-key",
        )
    )
    assert first.success is False
    assert first.error_code.value == "UPSTREAM_TIMEOUT"
    with _session(pg_engine) as s:
        row = s.execute(select(RefundRow)).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert row.status == "failed"
    assert order.reserved_amount == Decimal("0")  # FAILED releases
    assert order.refunded_amount == Decimal("0")

    second = asyncio.run(
        deps.tools.create_refund(
            ctx, order_id=PG_ORDER_ID, amount=Decimal("30.00"),
            issue_type=IssueType.REFUND_REQUEST, idempotency_key="retry-key",
        )
    )
    assert second.success is True
    assert gateway.execute_calls == 2  # the retry re-executes, deliberately
    with _session(pg_engine) as s:
        row = s.execute(select(RefundRow)).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert row.status == "processed"
    assert order.refunded_amount == Decimal("30.00")


def test_rejected_refund_releases_reservation(pg_engine, pg_url, monkeypatch):
    gateway = SimulatedPaymentGateway(outcomes=[GatewayOutcome.REJECTED])
    deps = _pg_deps(monkeypatch, pg_url, gateway=gateway)
    _make_run(deps, PG_TICKET_ID, "RUN-REJECTED")
    ctx = ToolContext(ticket_id=PG_TICKET_ID, run_id="RUN-REJECTED")

    result = asyncio.run(
        deps.tools.create_refund(
            ctx, order_id=PG_ORDER_ID, amount=Decimal("40.00"),
            issue_type=IssueType.REFUND_REQUEST, idempotency_key="reject-key",
        )
    )
    assert result.success is False
    assert result.error_code.value == "GATEWAY_REJECTED"
    with _session(pg_engine) as s:
        row = s.execute(select(RefundRow)).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert row.status == "rejected"
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("0")


# ---------------------------------------------------------------------
# D: safety regressions on the wired PG store
# ---------------------------------------------------------------------


def test_concurrent_creators_leave_exactly_one_pending_refund(
    pg_engine, pg_url, monkeypatch
):
    deps = _pg_deps(monkeypatch, pg_url)
    store: PgRefundStore = deps.tools.refund_store
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def creator(name: str, key: str) -> None:
        barrier.wait()
        outcome = store.create(
            None,
            order_id=PG_ORDER_ID,
            amount=Decimal("50.00"),
            idempotency_key=key,
            ticket_id=PG_TICKET_ID,
            issue_type=IssueType.REFUND_REQUEST,
            plan_id=None,
            run_id=None,
            reason="concurrency regression",
            ticket_claimed_amount=Decimal("199.99"),
            now=datetime.now(timezone.utc),
        )
        results[name] = outcome.kind

    threads = [
        threading.Thread(target=creator, args=("a", "race-key-a")),
        threading.Thread(target=creator, args=("b", "race-key-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _session(pg_engine) as s:
        rows = s.execute(select(RefundRow)).scalars().all()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert sorted(results.values()) == ["concurrent", "execute"]
    assert order.reserved_amount == Decimal("50.00")


def test_audit_and_refund_commit_atomically(pg_engine, pg_url, monkeypatch):
    deps = _pg_deps(monkeypatch, pg_url)
    store: PgRefundStore = deps.tools.refund_store

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(store, "_audit_row", boom)
    with pytest.raises(RuntimeError, match="audit write failed"):
        store.create(
            None,
            order_id=PG_ORDER_ID,
            amount=Decimal("10.00"),
            idempotency_key="atomicity-key",
            ticket_id=PG_TICKET_ID,
            issue_type=IssueType.REFUND_REQUEST,
            plan_id=None,
            run_id=None,
            reason="atomicity regression",
            ticket_claimed_amount=Decimal("199.99"),
            now=datetime.now(timezone.utc),
        )
    # The transaction rolled back: neither the refund nor a partial audit
    # row survives, and no balance was reserved.
    with _session(pg_engine) as s:
        assert s.execute(select(RefundRow)).scalars().all() == []
        orphan = s.execute(
            text(
                "SELECT COUNT(*) FROM audit_events a "
                "WHERE a.details->>'idempotency_key' = 'atomicity-key'"
            )
        ).scalar_one()
        order = s.get(OrderRow, PG_ORDER_ID)
    assert orphan == 0
    assert order.reserved_amount == Decimal("0")


def test_audit_trail_never_references_missing_refunds(
    pg_engine, pg_url, monkeypatch
):
    deps = _pg_deps(monkeypatch, pg_url)
    asyncio.run(run_ticket(PG_TICKET_ID, deps))

    with _session(pg_engine) as s:
        # Every audit event that names a refund must reference a refund
        # that actually exists.
        orphans = s.execute(
            text(
                "SELECT COUNT(*) FROM audit_events a "
                "WHERE a.details->>'refund_id' IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM refunds r "
                "WHERE r.refund_id = a.details->>'refund_id')"
            )
        ).scalar_one()
        # Every refund has a store-level audit event in the same
        # transaction (create + finalize == 2 events for 1 processed row).
        refunds = s.execute(select(RefundRow)).scalars().all()
        events = s.execute(
            select(AuditEventRow).where(
                AuditEventRow.action.in_(
                    ("refund_store.create", "refund_store.finalize",
                     "refund_store.reopen")
                )
            )
        ).scalars().all()
    assert orphans == 0
    assert len(refunds) == 1
    assert len(events) == 2
