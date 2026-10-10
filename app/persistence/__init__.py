"""Persistence layer.

Day 1: SQLAlchemy ``Base`` (PostgreSQL schema foundation).
Day 2: in-memory stores with explicit atomic sections plus the
deterministic mock catalog. The in-memory stores are test stores — the
PostgreSQL-verified guarantees land on Day 4.
"""

from app.persistence.base import Base

__all__ = ["Base"]
