"""SQLAlchemy declarative base.

Day 1 pins the dependency and the base class only. Later days add ORM
models mapped to PostgreSQL (via ``SUPPORTFLOW_DATABASE_URL``) without
restructuring imports elsewhere.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all future ORM models."""
