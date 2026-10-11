"""PostgreSQL integration test fixtures.

Safety rules enforced here:

- tests run ONLY when ``SUPPORTFLOW_TEST_DATABASE_URL`` is set and its
  database name ends with ``_test`` — a misconfigured URL fails the run
  instead of executing against a development or production database;
- the schema comes from Alembic migrations (never create_all); the
  migration is applied twice to prove idempotency;
- cleanup is TRUNCATE on the test database only, before every test.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.persistence.pg_refund_store import PgRefundStore

REPO_ROOT = Path(__file__).resolve().parents[2]


def _test_db_url() -> str:
    url = os.environ.get("SUPPORTFLOW_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip(
            "SUPPORTFLOW_TEST_DATABASE_URL is not set; PostgreSQL integration "
            "tests require a dedicated test database"
        )
    dbname = make_url(url).database or ""
    if not dbname.endswith("_test"):
        pytest.fail(
            f"refusing to run integration tests on database {dbname!r}: the "
            "database name must end with '_test' (guard against running "
            "against development/production data)"
        )
    return url


@pytest.fixture(scope="session")
def pg_url() -> str:
    return _test_db_url()


@pytest.fixture(scope="session")
def pg_engine(pg_url: str):
    engine = create_engine(pg_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # second run must be a no-op (idempotency)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_store(pg_engine):
    return PgRefundStore(sessionmaker(bind=pg_engine, expire_on_commit=False))


@pytest.fixture(autouse=True)
def clean_tables(pg_engine):
    """Empty the dedicated test database before each test. The database
    name guard in ``_test_db_url`` is what makes this safe."""
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_events, refunds, approvals, plans, "
                "agent_runs, ticket_notes, tickets, orders "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield
