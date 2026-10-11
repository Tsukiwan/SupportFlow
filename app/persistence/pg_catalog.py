"""Idempotent catalog seed for the PostgreSQL backend.

The deterministic mock catalog is the app's only order/ticket data
source. The postgres backend imports it at wiring time so that
:class:`~app.persistence.pg_refund_store.PgRefundStore` has ticket rows
to lock and foreign keys to reference. Seeding is idempotent and never
overwrites: existing rows — including balances already changed by
refunds — are left untouched, so re-wiring or re-running the demo cannot
reset persisted state.

The catalog's ``non_existent`` ticket is deliberately skipped: it binds
an order that does not exist, and PostgreSQL's referential integrity
makes that row unrepresentable. Memory mode keeps the scenario; the
postgres demo documents this (see README, "持久化边界").
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import Order, Ticket
from app.persistence.db import session_scope
from app.persistence.mock_catalog import (
    build_orders_by_id,
    build_tickets_by_id,
)
from app.persistence.models import OrderRow, TicketRow


def catalog_orders_and_tickets() -> tuple[list[Order], list[Ticket]]:
    """Catalog rows representable under PostgreSQL referential integrity:
    every order, and every ticket whose order exists. Tickets bound to a
    missing order are excluded."""
    orders = list(build_orders_by_id().values())
    order_ids = {order.order_id for order in orders}
    tickets = [
        ticket
        for ticket in build_tickets_by_id().values()
        if ticket.order_id in order_ids
    ]
    return orders, tickets


def ensure_catalog_seeded(session_factory: sessionmaker[Session]) -> None:
    """Insert the catalog into PG once, ignoring rows that already exist."""
    orders, tickets = catalog_orders_and_tickets()
    with session_scope(session_factory) as s:
        s.execute(
            pg_insert(OrderRow)
            .values(
                [
                    {
                        "order_id": o.order_id,
                        "customer_id": o.customer_id,
                        "status": o.status.value,
                        "currency": o.currency,
                        "total_amount": o.total_amount,
                        "refunded_amount": o.refunded_amount,
                        "reserved_amount": o.reserved_amount,
                        "created_at": o.created_at,
                        "delivered_at": o.delivered_at,
                    }
                    for o in orders
                ]
            )
            .on_conflict_do_nothing(index_elements=[OrderRow.order_id])
        )
        s.execute(
            pg_insert(TicketRow)
            .values(
                [
                    {
                        "ticket_id": t.ticket_id,
                        "order_id": t.order_id,
                        "customer_id": t.customer_id,
                        "status": t.status.value,
                        "issue_type": t.issue_type.value,
                        "risk_level": t.risk_level.value,
                        "subject": t.subject,
                        "description": t.description,
                        "claimed_amount": t.claimed_amount,
                        "created_at": t.created_at,
                        "updated_at": t.updated_at,
                    }
                    for t in tickets
                ]
            )
            .on_conflict_do_nothing(index_elements=[TicketRow.ticket_id])
        )
