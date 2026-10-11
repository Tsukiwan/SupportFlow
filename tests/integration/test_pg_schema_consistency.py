"""Day 5 E1: the schema produced by ``alembic upgrade head`` matches the
ORM models.

The integration ``pg_engine`` fixture migrates an empty test database to
``head`` twice (proving idempotency); this test then compares the live
schema against ``Base.metadata`` — every ORM table must exist with the
same column set, so a model change that forgets its migration fails here
instead of at runtime. Types are compared by name only (String/VARCHAR
etc.); precision details are exercised by the store tests themselves.
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from app.persistence.base import Base


def test_pg_schema_matches_orm_models(pg_engine):
    inspector = inspect(pg_engine)
    pg_tables = set(inspector.get_table_names())
    orm_tables = set(Base.metadata.tables)

    assert orm_tables <= pg_tables, (
        f"ORM tables missing from the migrated schema: "
        f"{sorted(orm_tables - pg_tables)} — a model was added without a migration"
    )

    for table_name in sorted(orm_tables):
        pg_columns = {
            col["name"] for col in inspector.get_columns(table_name)
        }
        orm_columns = set(Base.metadata.tables[table_name].columns.keys())
        assert orm_columns == pg_columns, (
            f"column drift on {table_name}: "
            f"pg-only={sorted(pg_columns - orm_columns)}, "
            f"orm-only={sorted(orm_columns - pg_columns)}"
        )


def test_orm_types_match_migrated_types(pg_engine):
    """Column type names must agree for the safety-relevant columns
    (MONEY, TIMESTAMP, status widths)."""
    inspector = inspect(pg_engine)
    pg_columns = {
        (table, col["name"]): col["type"]
        for table in inspector.get_table_names()
        for col in inspector.get_columns(table)
    }
    pg_dialect = sa.dialects.postgresql.dialect()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            pg_type = pg_columns[(table.name, column.name)]
            actual = pg_type.compile(dialect=pg_dialect).upper()
            expected = column.type.compile(dialect=pg_dialect).upper()
            assert actual == expected, (
                f"type drift on {table.name}.{column.name}: "
                f"pg={actual}, orm={expected}"
            )
