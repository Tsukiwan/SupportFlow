"""PgRefundStore against a real PostgreSQL database.

Every test drives the store over independent sessions/connections and
asserts final database state (rows, balances, reservations, audit
events), not just return values. Concurrency tests (C14-C19) run two
separate *processes* synchronized by a multiprocessing barrier — never
sleeps. The gateway is not involved: the store's contract ends at
PENDING; finalize models the gateway result application.

The whole module is skipped unless SUPPORTFLOW_TEST_DATABASE_URL points
at a database whose name ends with ``_test`` (see conftest).
"""

import multiprocessing
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import IssueType, RefundStatus
from app.persistence.models import (
    AgentRunRow,
    AuditEventRow,
    OrderRow,
    PlanRow,
    RefundRow,
    TicketRow,
)
from app.persistence.pg_refund_store import PgRefundStore

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# seeding and query helpers (independent sessions)
# ----------------------------------------------------------------------


def seed(
    engine,
    *,
    order_id="ORD-1",
    total="500.00",
    refunded="0",
    reserved="0",
    ticket_id="TKT-1",
    claimed="500.00",
    plan_id="PLAN-1",
    issue_type="refund_request",
    run_id="RUN-1",
):
    with Session(engine) as s:
        # Insert in FK-dependency tiers with explicit flushes. The ORM
        # unit of work only orders INSERTs by declared relationship(),
        # which these models do not define — PostgreSQL enforces the
        # ForeignKeys immediately, so one combined flush can emit
        # children before their parents (plans before tickets).
        s.add(
            OrderRow(
                order_id=order_id,
                customer_id="CUS-1",
                status="delivered",
                currency="USD",
                total_amount=Decimal(total),
                refunded_amount=Decimal(refunded),
                reserved_amount=Decimal(reserved),
                created_at=T0,
                delivered_at=T0,
            )
        )
        s.flush()
        s.add(
            TicketRow(
                ticket_id=ticket_id,
                order_id=order_id,
                customer_id="CUS-1",
                status="open",
                issue_type=issue_type,
                risk_level="low",
                subject="Integration ticket",
                description="PostgreSQL integration test ticket.",
                claimed_amount=Decimal(claimed),
                created_at=T0,
                updated_at=T0,
            )
        )
        s.flush()
        s.add(
            PlanRow(
                plan_id=plan_id,
                ticket_id=ticket_id,
                content={},
                content_hash="h",
                created_at=T0,
            )
        )
        s.flush()
        # audit_events.run_id is FK-bound to agent_runs, so every store
        # operation that audits under RUN-1 needs this row. Multiple
        # seeds (one per order in multi-order tests) share one run.
        if s.get(AgentRunRow, run_id) is None:
            s.add(
                AgentRunRow(
                    run_id=run_id,
                    ticket_id=ticket_id,
                    status="running",
                    started_at=T0,
                )
            )
        s.commit()


def create_refund(
    store,
    *,
    order_id="ORD-1",
    amount="60.00",
    key="key-1",
    ticket_id="TKT-1",
    plan_id="PLAN-1",
    run_id="RUN-1",
    reason="Deterministic integration reason.",
    claimed="500.00",
):
    return store.create(
        None,
        order_id=order_id,
        amount=Decimal(amount),
        idempotency_key=key,
        ticket_id=ticket_id,
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=plan_id,
        run_id=run_id,
        reason=reason,
        ticket_claimed_amount=Decimal(claimed),
        now=T0,
    )


def fetch_refund(engine, key):
    with Session(engine) as s:
        return s.execute(
            select(RefundRow).where(RefundRow.idempotency_key == key)
        ).scalar_one_or_none()


def fetch_order(engine, order_id="ORD-1"):
    with Session(engine) as s:
        return s.execute(
            select(OrderRow).where(OrderRow.order_id == order_id)
        ).scalar_one_or_none()


def count_refunds(engine):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(RefundRow))


def fetch_audits(engine, action=None):
    with Session(engine) as s:
        stmt = select(AuditEventRow)
        if action is not None:
            stmt = stmt.where(AuditEventRow.action == action)
        return s.execute(stmt).scalars().all()


# ----------------------------------------------------------------------
# A. basic transactions and balances
# ----------------------------------------------------------------------


def test_create_persists_pending_reservation_and_audit(pg_store, pg_engine):
    seed(pg_engine)
    outcome = create_refund(pg_store, amount="60.00")
    assert outcome.kind == "execute"

    row = fetch_refund(pg_engine, "key-1")
    assert row is not None and row.status == RefundStatus.PENDING.value
    order = fetch_order(pg_engine)
    assert order.reserved_amount == Decimal("60.00")
    assert order.refunded_amount == Decimal("0")
    audits = fetch_audits(pg_engine, "refund_store.create")
    assert len(audits) == 1
    assert audits[0].details["refund_id"] == row.refund_id

    updated = pg_store.finalize(
        row.refund_id, RefundStatus.PROCESSED, orders=None, now=T0
    )
    assert updated is not None and updated.status is RefundStatus.PROCESSED
    order = fetch_order(pg_engine)
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("60.00")
    row = fetch_refund(pg_engine, "key-1")
    assert row.status == RefundStatus.PROCESSED.value
    assert row.processed_at is not None
    assert len(fetch_audits(pg_engine, "refund_store.finalize")) == 1


def test_explicit_failure_releases_reservation_and_audits(pg_store, pg_engine):
    seed(pg_engine)
    outcome = create_refund(pg_store)
    assert outcome.kind == "execute"
    updated = pg_store.finalize(
        outcome.refund.refund_id, RefundStatus.FAILED, orders=None, now=T0
    )
    assert updated is not None and updated.status is RefundStatus.FAILED
    order = fetch_order(pg_engine)
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("0")
    assert len(fetch_audits(pg_engine, "refund_store.finalize")) == 1


def test_unknown_keeps_reservation_and_blocks_same_order(pg_store, pg_engine):
    seed(pg_engine)
    first = create_refund(pg_store, key="key-a")
    assert first.kind == "execute"
    pg_store.finalize(first.refund.refund_id, RefundStatus.UNKNOWN, orders=None, now=T0)

    order = fetch_order(pg_engine)
    assert order.reserved_amount == Decimal("60.00")  # reservation kept

    blocked = create_refund(pg_store, key="key-b", amount="20.00")
    assert blocked.kind == "unresolved"
    assert blocked.refund is not None
    assert blocked.refund.idempotency_key == "key-a"  # the blocking record
    assert count_refunds(pg_engine) == 1  # the blocked create inserted nothing
    assert fetch_order(pg_engine).reserved_amount == Decimal("60.00")


def test_insufficient_balance_commits_nothing(pg_store, pg_engine):
    seed(pg_engine, total="100.00", refunded="60.00")
    outcome = create_refund(pg_store, amount="50.00")
    assert outcome.kind == "balance_exceeded"
    assert count_refunds(pg_engine) == 0
    order = fetch_order(pg_engine)
    assert order.reserved_amount == Decimal("0")
    assert order.refunded_amount == Decimal("60.00")
    assert len(fetch_audits(pg_engine)) == 0


def test_audit_insert_failure_rolls_back_the_whole_transaction(pg_store, pg_engine):
    """The audit event is FK-bound to agent_runs; a run_id that has no run
    row makes the audit insert fail. The refund row and the reservation
    created in the same transaction must roll back with it."""
    seed(pg_engine)
    with pytest.raises(IntegrityError):
        create_refund(pg_store, run_id="RUN-NOPE")  # no agent_runs row
    assert count_refunds(pg_engine) == 0
    assert fetch_order(pg_engine).reserved_amount == Decimal("0")
    assert len(fetch_audits(pg_engine)) == 0


def test_invalid_amount_rejected_without_partial_state(pg_store, pg_engine):
    seed(pg_engine)
    with pytest.raises(IntegrityError):  # ck_refunds_amount_positive
        create_refund(pg_store, amount="0")
    assert count_refunds(pg_engine) == 0
    assert fetch_order(pg_engine).reserved_amount == Decimal("0")


def test_finalize_refuses_invalid_transition_targets(pg_store, pg_engine):
    seed(pg_engine)
    outcome = create_refund(pg_store)
    assert outcome.kind == "execute"
    updated = pg_store.finalize(
        outcome.refund.refund_id, RefundStatus.APPROVED, orders=None, now=T0
    )
    assert updated is None
    row = fetch_refund(pg_engine, "key-1")
    assert row.status == RefundStatus.PENDING.value  # unchanged
    assert fetch_order(pg_engine).reserved_amount == Decimal("60.00")


def test_finalize_never_settles_twice(pg_store, pg_engine):
    seed(pg_engine)
    outcome = create_refund(pg_store)
    assert pg_store.finalize(
        outcome.refund.refund_id, RefundStatus.PROCESSED, orders=None, now=T0
    ) is not None
    assert (
        pg_store.finalize(
            outcome.refund.refund_id, RefundStatus.PROCESSED, orders=None, now=T0
        )
        is None
    )
    order = fetch_order(pg_engine)
    assert order.refunded_amount == Decimal("60.00")  # settled once, not 120
    assert order.reserved_amount == Decimal("0")


# ----------------------------------------------------------------------
# B. idempotency and retries
# ----------------------------------------------------------------------


def test_same_key_same_intent_never_creates_a_second_row(pg_store, pg_engine):
    seed(pg_engine)
    first = create_refund(pg_store)
    assert first.kind == "execute"
    # Same key while the first is PENDING: concurrent, not a new row.
    again = create_refund(pg_store)
    assert again.kind == "concurrent"
    assert count_refunds(pg_engine) == 1
    # Same key after processing: already_processed, still one row.
    pg_store.finalize(first.refund.refund_id, RefundStatus.PROCESSED, orders=None, now=T0)
    again = create_refund(pg_store)
    assert again.kind == "already_processed"
    assert count_refunds(pg_engine) == 1


def test_same_key_different_intent_conflicts(pg_store, pg_engine):
    seed(pg_engine)
    assert create_refund(pg_store, amount="50.00").kind == "execute"
    conflict = create_refund(pg_store, amount="60.00")
    assert conflict.kind == "conflict"
    row = fetch_refund(pg_engine, "key-1")
    assert row.amount == Decimal("50.00")  # the original intent stands
    assert count_refunds(pg_engine) == 1


def test_different_key_same_order_pending_blocked(pg_store, pg_engine):
    seed(pg_engine)
    assert create_refund(pg_store, key="key-a").kind == "execute"
    blocked = create_refund(pg_store, key="key-b")
    assert blocked.kind == "concurrent"
    assert count_refunds(pg_engine) == 1


def test_different_key_same_order_unknown_blocked(pg_store, pg_engine):
    seed(pg_engine)
    first = create_refund(pg_store, key="key-a")
    pg_store.finalize(first.refund.refund_id, RefundStatus.UNKNOWN, orders=None, now=T0)
    blocked = create_refund(pg_store, key="key-b")
    assert blocked.kind == "unresolved"
    assert blocked.refund.idempotency_key == "key-a"


def test_failed_retry_rechecks_the_claim_cap(pg_store, pg_engine):
    """A FAILED refund holds no claim room; a PENDING sibling carrying the
    same ticket from another order (so the same-order gate stays silent)
    must make the reopen fail the aggregate claim check without touching
    either order's balance."""
    seed(pg_engine, order_id="ORD-A", ticket_id="TKT-1", claimed="100.00")
    seed(pg_engine, order_id="ORD-B", ticket_id="TKT-2", plan_id="PLAN-2", claimed="100.00")
    failed = create_refund(
        pg_store, order_id="ORD-A", ticket_id="TKT-1", amount="50.00", key="key-a"
    )
    pg_store.finalize(failed.refund.refund_id, RefundStatus.FAILED, orders=None, now=T0)
    sibling = create_refund(
        pg_store,
        order_id="ORD-B",
        ticket_id="TKT-1",
        plan_id="PLAN-2",
        amount="60.00",
        key="key-b",
    )
    assert sibling.kind == "execute"

    kind, refund = pg_store.reopen(
        failed.refund.refund_id, orders=None, ticket_claimed_amount=Decimal("100.00")
    )
    assert kind == "claim_exceeded"
    assert refund is None
    assert fetch_refund(pg_engine, "key-a").status == RefundStatus.FAILED.value
    assert fetch_refund(pg_engine, "key-b").status == RefundStatus.PENDING.value
    assert fetch_order(pg_engine, "ORD-A").reserved_amount == Decimal("0")
    assert fetch_order(pg_engine, "ORD-B").reserved_amount == Decimal("60.00")


def test_failed_retry_blocked_by_pending_sibling(pg_store, pg_engine):
    seed(pg_engine)
    failed = create_refund(pg_store, key="key-a", amount="50.00")
    pg_store.finalize(failed.refund.refund_id, RefundStatus.FAILED, orders=None, now=T0)
    assert create_refund(pg_store, key="key-b", amount="60.00").kind == "execute"

    kind, refund = pg_store.reopen(
        failed.refund.refund_id, orders=None, ticket_claimed_amount=Decimal("500.00")
    )
    assert kind == "concurrent"
    assert refund is None
    assert fetch_refund(pg_engine, "key-a").status == RefundStatus.FAILED.value
    assert fetch_refund(pg_engine, "key-b").status == RefundStatus.PENDING.value
    assert fetch_order(pg_engine).reserved_amount == Decimal("60.00")  # only B's


def test_failed_retry_blocked_by_unknown_sibling(pg_store, pg_engine):
    seed(pg_engine)
    failed = create_refund(pg_store, key="key-a", amount="50.00")
    pg_store.finalize(failed.refund.refund_id, RefundStatus.FAILED, orders=None, now=T0)
    sibling = create_refund(pg_store, key="key-b", amount="60.00")
    pg_store.finalize(
        sibling.refund.refund_id, RefundStatus.UNKNOWN, orders=None, now=T0
    )

    kind, refund = pg_store.reopen(
        failed.refund.refund_id, orders=None, ticket_claimed_amount=Decimal("500.00")
    )
    assert kind == "unresolved"
    assert refund is not None and refund.idempotency_key == "key-b"
    assert fetch_refund(pg_engine, "key-a").status == RefundStatus.FAILED.value
    assert fetch_refund(pg_engine, "key-b").status == RefundStatus.UNKNOWN.value
    assert fetch_order(pg_engine).reserved_amount == Decimal("60.00")


def test_successful_retry_reuses_the_single_row(pg_store, pg_engine):
    seed(pg_engine)
    failed = create_refund(pg_store, amount="50.00")
    pg_store.finalize(failed.refund.refund_id, RefundStatus.FAILED, orders=None, now=T0)

    kind, refund = pg_store.reopen(
        failed.refund.refund_id, orders=None, ticket_claimed_amount=Decimal("500.00")
    )
    assert kind == "reopened"
    assert refund is not None and refund.status is RefundStatus.PENDING
    assert refund.refund_id == failed.refund.refund_id  # same record
    assert fetch_order(pg_engine).reserved_amount == Decimal("50.00")

    settled = pg_store.finalize(refund.refund_id, RefundStatus.PROCESSED, orders=None, now=T0)
    assert settled is not None
    assert count_refunds(pg_engine) == 1
    assert fetch_refund(pg_engine, "key-1").status == RefundStatus.PROCESSED.value
    order = fetch_order(pg_engine)
    assert order.refunded_amount == Decimal("50.00")
    assert order.reserved_amount == Decimal("0")


# ----------------------------------------------------------------------
# C. concurrency: two separate processes, one barrier, final DB state
# ----------------------------------------------------------------------


def _create_worker(url, barrier, payload, queue):
    """One process: build its own engine/session, wait at the barrier,
    then run one store.create. PgRefundStore opens its own connections."""
    engine = create_engine(url)
    try:
        store = PgRefundStore(sessionmaker(bind=engine, expire_on_commit=False))
        barrier.wait(timeout=60)
        outcome = store.create(None, **payload)
        refund = outcome.refund
        queue.put(
            (
                outcome.kind,
                refund.refund_id if refund is not None else None,
                str(refund.amount) if refund is not None else None,
            )
        )
    except Exception as exc:  # surface worker failures as results
        queue.put(("worker_error", str(exc), None))
    finally:
        engine.dispose()


def _finalize_worker(url, barrier, refund_id, queue):
    engine = create_engine(url)
    try:
        store = PgRefundStore(sessionmaker(bind=engine, expire_on_commit=False))
        barrier.wait(timeout=60)
        updated = store.finalize(
            refund_id,
            RefundStatus.PROCESSED,
            orders=None,
            now=datetime.now(timezone.utc),
        )
        queue.put("updated" if updated is not None else "none")
    except Exception as exc:
        queue.put(f"worker_error: {exc}")
    finally:
        engine.dispose()


def _run_pair(pg_url, payloads, worker=_create_worker):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(3)  # two workers + the parent
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=worker, args=(pg_url, barrier, payload, queue))
        for payload in payloads
    ]
    for p in procs:
        p.start()
    barrier.wait(timeout=60)
    for p in procs:
        p.join(timeout=180)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"
    return [queue.get(timeout=30) for _ in procs]


def _payloads(key_a, key_b, amount_a="60.00", amount_b="60.00", **common):
    base = dict(
        order_id="ORD-1",
        ticket_id="TKT-1",
        plan_id="PLAN-1",
        run_id="RUN-1",
        issue_type=IssueType.REFUND_REQUEST,
        ticket_claimed_amount=Decimal("500.00"),
        now=T0,
    )
    base.update(common)
    return [
        dict(
            base,
            idempotency_key=key_a,
            amount=Decimal(amount_a),
            reason="Concurrent refund A in a separate process.",
        ),
        dict(
            base,
            idempotency_key=key_b,
            amount=Decimal(amount_b),
            reason="Concurrent refund B in a separate process.",
        ),
    ]


def test_two_processes_one_pending_per_order(pg_url, pg_engine):
    seed(pg_engine)
    results = _run_pair(
        pg_url,
        _payloads("key-a", "key-b"),
    )
    kinds = sorted(r[0] for r in results)
    assert kinds == ["concurrent", "execute"], results
    assert count_refunds(pg_engine) == 1
    assert fetch_order(pg_engine).reserved_amount == Decimal("60.00")


def test_two_processes_same_key_never_duplicate(pg_url, pg_engine):
    seed(pg_engine)
    results = _run_pair(pg_url, _payloads("same-key", "same-key"))
    kinds = sorted(r[0] for r in results)
    assert kinds == ["concurrent", "execute"], results
    assert count_refunds(pg_engine) == 1  # one row, never two


def test_two_processes_same_key_different_intent(pg_url, pg_engine):
    seed(pg_engine)
    results = _run_pair(
        pg_url,
        _payloads("same-key", "same-key", amount_a="50.00", amount_b="60.00"),
    )
    kinds = sorted(r[0] for r in results)
    assert kinds == ["conflict", "execute"], results
    winner = next(r for r in results if r[0] == "execute")
    row = fetch_refund(pg_engine, "same-key")
    assert row is not None and str(row.amount) == winner[2]
    assert count_refunds(pg_engine) == 1


def test_two_processes_claim_cap(pg_url, pg_engine):
    """One ticket with claim 100.00 shared by two orders, 60.00 each: the
    ticket row lock serializes the aggregate claim check, so exactly one
    refund may occupy the claim and the loser is claim_exceeded."""
    seed(pg_engine, order_id="ORD-A", ticket_id="TKT-A", plan_id="PLAN-A", claimed="100.00")
    seed(pg_engine, order_id="ORD-B", ticket_id="TKT-B", plan_id="PLAN-B", claimed="100.00")
    payloads = _payloads(
        "key-a", "key-b", ticket_claimed_amount=Decimal("100.00")
    )
    payloads[0].update(order_id="ORD-A", ticket_id="TKT-A", plan_id="PLAN-A")
    payloads[1].update(order_id="ORD-B", ticket_id="TKT-A", plan_id="PLAN-B")
    results = _run_pair(pg_url, payloads)
    kinds = sorted(r[0] for r in results)
    assert kinds == ["claim_exceeded", "execute"], results
    assert count_refunds(pg_engine) == 1
    assert fetch_order(pg_engine, "ORD-A").reserved_amount + fetch_order(
        pg_engine, "ORD-B"
    ).reserved_amount == Decimal("60.00")


def test_two_processes_finalize_no_double_settle(pg_url, pg_engine):
    seed(pg_engine)
    store = PgRefundStore(sessionmaker(bind=pg_engine, expire_on_commit=False))
    created = create_refund(store)
    refund_id = created.refund.refund_id
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(3)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_finalize_worker, args=(pg_url, barrier, refund_id, queue)
        )
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    barrier.wait(timeout=60)
    for p in procs:
        p.join(timeout=180)
        assert p.exitcode == 0
    results = sorted(queue.get(timeout=30) for _ in procs)
    assert results == ["none", "updated"], results
    row = fetch_refund(pg_engine, "key-1")
    assert row.status == RefundStatus.PROCESSED.value
    order = fetch_order(pg_engine)
    assert order.refunded_amount == Decimal("60.00")  # settled exactly once
    assert order.reserved_amount == Decimal("0")


def test_two_processes_two_orders_balance_invariants(pg_url, pg_engine):
    seed(pg_engine, order_id="ORD-A", ticket_id="TKT-A", plan_id="PLAN-A", claimed="100.00")
    seed(pg_engine, order_id="ORD-B", ticket_id="TKT-B", plan_id="PLAN-B", claimed="100.00")
    payloads = _payloads("key-a", "key-b", amount_a="100.00", amount_b="100.00")
    payloads[0].update(order_id="ORD-A", ticket_id="TKT-A", plan_id="PLAN-A")
    payloads[1].update(order_id="ORD-B", ticket_id="TKT-B", plan_id="PLAN-B")
    results = _run_pair(pg_url, payloads)
    assert sorted(r[0] for r in results) == ["execute", "execute"], results
    for order_id in ("ORD-A", "ORD-B"):
        order = fetch_order(pg_engine, order_id)
        assert order.reserved_amount == Decimal("100.00")
        assert order.refunded_amount == Decimal("0")
        assert order.reserved_amount + order.refunded_amount <= order.total_amount
    assert count_refunds(pg_engine) == 2
