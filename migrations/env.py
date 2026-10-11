"""Alembic environment for SupportFlow.

Resolves the database URL (``-x db_url`` argument wins, then the ini
option, then ``SUPPORTFLOW_DATABASE_URL`` from app settings) and supports
both offline (``--sql``, no database connection) and online modes. A
migration run uses a NullPool: it opens one short-lived connection and
never holds pooled connections across the process exit.
"""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.persistence import models  # noqa: F401 — registers tables on Base
from app.persistence.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db_url"):
        return x_args["db_url"]
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Render the migration as SQL without connecting (``--sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migration against a live database."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
