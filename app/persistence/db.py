"""Database engine and session factory for the persistence layer.

The backend is chosen explicitly by ``SUPPORTFLOW_DATABASE_URL``: the URL
decides the dialect (``postgresql+psycopg://`` for the Day 4 store,
``sqlite://`` for dependency-free local exploration). There is
deliberately no try/except fallback — a connection failure raises and
surfaces to the caller, it never silently swaps to another backend and
never falls back to the in-memory stores.

Session lifecycle (binding for the SQL store built in the next batch):
one short-lived :class:`Session` per operation, created by
:func:`session_scope` or the factory. Sessions are not shared across
requests or tasks, and are never held across an ``await`` that reaches
the payment gateway — a refund transaction commits before any gateway
call, and gateway results are applied in a fresh session.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def build_engine(settings: Settings) -> Engine:
    """Engine from settings. ``pool_pre_ping`` drops dead connections
    after a database restart instead of handing them to callers."""
    return create_engine(settings.database_url, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory. ``expire_on_commit=False`` keeps attributes
    readable after commit, matching the store's short-lived usage."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit-on-success, rollback-on-error scope for one operation."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
