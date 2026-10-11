"""Day 5 wiring tests (unit level — these are NOT integration tests).

The PostgreSQL store objects are constructed over a lazily-created engine
that is never connected: ``create_engine``/``sessionmaker`` are deferred,
and none of the constructors under test open a session, so no database
connection is ever made here. Real PostgreSQL behavior is covered by
``tests/integration/`` (guarded by ``SUPPORTFLOW_TEST_DATABASE_URL``).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.runner import build_agent_wiring
from app.config import Settings
from app.persistence.memory_stores import (
    AgentRunStore,
    ApprovalStore,
    AuditStore,
    OrderStore,
    RefundStore,
    TicketStore,
)
from app.persistence.pg_catalog import catalog_orders_and_tickets
from app.persistence.pg_refund_store import PgRefundStore
from app.persistence.pg_stores import (
    PgAuditStore,
    PgOrderStore,
    PgPlanStore,
)
from app.services.plans import PlanStore
from app.tools.registry import build_pg_tool_set

PG_URL = "postgresql+psycopg://user:pass@127.0.0.1:59999/never_connected"


def _pg_settings(monkeypatch) -> Settings:
    """Settings for the postgres backend, via env vars (the repo
    convention for aliased Settings fields; ``_env_file=None`` isolates
    from the developer's .env)."""
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_URL", PG_URL)
    monkeypatch.setenv("SUPPORTFLOW_STORE_BACKEND", "postgres")
    return Settings(_env_file=None)


def _lazy_session_factory() -> sessionmaker:
    """A session factory over an engine that is never connected."""
    return sessionmaker(
        bind=create_engine(PG_URL, pool_pre_ping=False),
        expire_on_commit=False,
    )


# --- backend selection (config) ---


def test_store_backend_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("SUPPORTFLOW_STORE_BACKEND", raising=False)
    settings = Settings(_env_file=None)
    assert settings.store_backend == "memory"


def test_postgres_backend_without_postgres_url_fails_at_startup(monkeypatch):
    # Explicit configuration error — never a silent fallback to memory.
    monkeypatch.setenv("SUPPORTFLOW_STORE_BACKEND", "postgres")
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_URL", "postgresql://plain")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_postgres_backend_with_psycopg_url_is_accepted(monkeypatch):
    settings = _pg_settings(monkeypatch)
    assert settings.store_backend == "postgres"
    assert settings.database_url == PG_URL


# --- memory wiring stays the default ---


def test_memory_backend_builds_memory_stores(monkeypatch):
    monkeypatch.delenv("SUPPORTFLOW_STORE_BACKEND", raising=False)
    monkeypatch.delenv("SUPPORTFLOW_DATABASE_URL", raising=False)
    deps = build_agent_wiring(settings=Settings(_env_file=None))
    tools = deps.tools
    assert isinstance(tools.orders, OrderStore)
    assert isinstance(tools.refund_store, RefundStore)
    assert isinstance(tools.audit_store, AuditStore)
    assert isinstance(deps.plans, PlanStore)
    assert isinstance(deps.runs, AgentRunStore)


# --- postgres wiring refuses to mix backends ---


def test_postgres_wiring_refuses_injected_store_contents(monkeypatch):
    with pytest.raises(TypeError, match="memory-mode test path"):
        build_agent_wiring(settings=_pg_settings(monkeypatch), orders={})


def test_postgres_wiring_refuses_injected_plan_store(monkeypatch):
    with pytest.raises(TypeError, match="owns the plan registry"):
        build_agent_wiring(settings=_pg_settings(monkeypatch), plans=PlanStore())


# --- the postgres tool set wires PG stores for the refund chain ---


def test_build_pg_tool_set_wires_pg_and_memory_stores():
    factory = _lazy_session_factory()
    tools = build_pg_tool_set(
        session_factory=factory, plans=PgPlanStore(factory)
    )
    # Refund chain: PG-backed.
    assert isinstance(tools.orders, PgOrderStore)
    assert isinstance(tools.refund_store, PgRefundStore)
    assert isinstance(tools.audit_store, PgAuditStore)
    assert isinstance(tools._refund_service._refunds, PgRefundStore)
    # Workflow state: the in-memory catalog.
    assert isinstance(tools.tickets, TicketStore)
    assert isinstance(tools.approvals, ApprovalStore)
    assert tools.shipments is not None
    assert tools.notes is not None


def test_pg_order_store_mutations_fail_loudly():
    factory = _lazy_session_factory()
    orders = PgOrderStore(factory)
    with pytest.raises(NotImplementedError, match="owned by PgRefundStore"):
        orders.reserve("ORD-1", Decimal("1.00"))
    with pytest.raises(NotImplementedError, match="owned by PgRefundStore"):
        orders.release("ORD-1", Decimal("1.00"))
    with pytest.raises(NotImplementedError, match="owned by PgRefundStore"):
        orders.settle("ORD-1", Decimal("1.00"))


# --- catalog seed rows (pure function, no database) ---


def test_catalog_seed_rows_skip_orderless_tickets():
    orders, tickets = catalog_orders_and_tickets()
    order_ids = {o.order_id for o in orders}
    # 6 catalog scenarios, one of which has no order at all.
    assert len(orders) == 5
    assert len(tickets) == 5
    ticket_ids = {t.ticket_id for t in tickets}
    assert "TKT-2006" not in ticket_ids  # bound to a missing order
    assert all(t.order_id in order_ids for t in tickets)


def test_catalog_seed_rows_are_stable():
    first = catalog_orders_and_tickets()
    second = catalog_orders_and_tickets()
    assert [o.order_id for o in first[0]] == [o.order_id for o in second[0]]
    assert [t.ticket_id for t in first[1]] == [t.ticket_id for t in second[1]]


# --- the agent wiring uses the PG run store in postgres mode is covered
# by tests/integration (it seeds the catalog, which needs a live database).
# The refusal paths above prove build_agent_wiring does not silently mix.
