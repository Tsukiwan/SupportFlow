"""Day 5 C: the unique constraints as *database* backstops, tested
directly — separately from the store's business-layer rejections.

The store refuses a second unresolved refund for the same order under
row locks (``concurrent``/``unresolved`` kinds). These tests prove the
constraints themselves hold against a writer that bypasses the store —
which is exactly the situation the partial unique index exists for:
- ``uq_refunds_order_unresolved`` — at most one PENDING|UNKNOWN refund
  per order, enforced by PostgreSQL;
- ``uq_refunds_idempotency_key`` — one row per idempotency key;
- and that the order index is *partial*: processed rows coexist.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.domain.enums import IssueType
from app.persistence.models import RefundRow
from app.persistence.pg_catalog import ensure_catalog_seeded

ORDER_ID = "ORD-1001"
TICKET_ID = "TKT-2001"


@pytest.fixture()
def seeded(pg_engine):
    ensure_catalog_seeded(sessionmaker(bind=pg_engine, expire_on_commit=False))


def _refund_values(*, refund_id: str, key: str, status: str) -> dict:
    return {
        "refund_id": refund_id,
        "idempotency_key": key,
        "ticket_id": TICKET_ID,
        "order_id": ORDER_ID,
        "amount": Decimal("50.00"),
        "currency": "USD",
        "issue_type": IssueType.REFUND_REQUEST.value,
        "plan_id": None,
        "run_id": None,
        "status": status,
        "reason": "constraint backstop test",
        "created_at": datetime.now(timezone.utc),
        "processed_at": None,
    }


def test_index_rejects_two_unresolved_refunds_for_one_order(
    pg_engine, seeded
):
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO refunds (refund_id, idempotency_key, ticket_id,"
                " order_id, amount, currency, issue_type, status, reason,"
                " created_at) VALUES (:rid, :key, :tid, :oid, 50.00, 'USD',"
                " 'refund_request', 'pending', 'first', now())"
            ),
            {"rid": "REF-DIRECT-1", "key": "direct-key-1",
             "tid": TICKET_ID, "oid": ORDER_ID},
        )
        with pytest.raises(IntegrityError) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO refunds (refund_id, idempotency_key,"
                    " ticket_id, order_id, amount, currency, issue_type,"
                    " status, reason, created_at) VALUES (:rid, :key, :tid,"
                    " :oid, 30.00, 'USD', 'refund_request', 'unknown',"
                    " 'second', now())"
                ),
                {"rid": "REF-DIRECT-2", "key": "direct-key-2",
                 "tid": TICKET_ID, "oid": ORDER_ID},
            )
    # The failure is identified by the index's name, not guessed from text.
    orig = excinfo.value.orig
    assert orig.diag.constraint_name == "uq_refunds_order_unresolved"


def test_index_rejects_duplicate_idempotency_key(pg_engine, seeded):
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO refunds (refund_id, idempotency_key, ticket_id,"
                " order_id, amount, currency, issue_type, status, reason,"
                " created_at) VALUES ('REF-KEY-1', :key, :tid, :oid, 10.00,"
                " 'USD', 'refund_request', 'processed', 'first', now())"
            ),
            {"key": "duplicate-key", "tid": TICKET_ID, "oid": ORDER_ID},
        )
        with pytest.raises(IntegrityError) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO refunds (refund_id, idempotency_key,"
                    " ticket_id, order_id, amount, currency, issue_type,"
                    " status, reason, created_at) VALUES ('REF-KEY-2', :key,"
                    " :tid, :oid, 10.00, 'USD', 'refund_request',"
                    " 'processed', 'second', now())"
                ),
                {"key": "duplicate-key", "tid": TICKET_ID, "oid": ORDER_ID},
            )
    assert excinfo.value.orig.diag.constraint_name == "uq_refunds_idempotency_key"


def test_order_index_is_partial_processed_rows_coexist(pg_engine, seeded):
    """The gate only covers unresolved refunds: several processed refunds
    for the same order are legitimate (partial refunds) and must not
    trip the index."""
    with pg_engine.begin() as conn:
        for rid, key, amount in (
            ("REF-PROC-1", "proc-key-1", "10.00"),
            ("REF-PROC-2", "proc-key-2", "20.00"),
        ):
            conn.execute(
                text(
                    "INSERT INTO refunds (refund_id, idempotency_key,"
                    " ticket_id, order_id, amount, currency, issue_type,"
                    " status, reason, created_at) VALUES (:rid, :key, :tid,"
                    " :oid, :amount, 'USD', 'refund_request', 'processed',"
                    " 'partial', now())"
                ),
                {"rid": rid, "key": key, "tid": TICKET_ID,
                 "oid": ORDER_ID, "amount": amount},
            )
    with sessionmaker(bind=pg_engine)() as s:
        assert len(s.execute(select(RefundRow)).scalars().all()) == 2


def test_store_rejects_second_pending_before_the_index_fires(
    pg_engine, seeded, monkeypatch
):
    """The business layer refuses under row locks — the index only backs
    up a writer that bypasses it (proved by the raw-INSERT test above).
    This is the store-level rejection, kept separate on purpose."""
    from app.persistence.pg_refund_store import PgRefundStore

    store = PgRefundStore(sessionmaker(bind=pg_engine, expire_on_commit=False))
    now = datetime.now(timezone.utc)
    first = store.create(
        None,
        order_id=ORDER_ID,
        amount=Decimal("50.00"),
        idempotency_key="store-key-1",
        ticket_id=TICKET_ID,
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=None,
        run_id=None,
        reason="store-layer gate",
        ticket_claimed_amount=Decimal("199.99"),
        now=now,
    )
    assert first.kind == "execute"
    second = store.create(
        None,
        order_id=ORDER_ID,
        amount=Decimal("30.00"),
        idempotency_key="store-key-2",
        ticket_id=TICKET_ID,
        issue_type=IssueType.REFUND_REQUEST,
        plan_id=None,
        run_id=None,
        reason="store-layer gate",
        ticket_claimed_amount=Decimal("199.99"),
        now=now,
    )
    assert second.kind == "concurrent"
    with sessionmaker(bind=pg_engine)() as s:
        rows = s.execute(select(RefundRow)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
