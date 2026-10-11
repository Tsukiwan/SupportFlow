"""SQLAlchemy ORM models: the Day 4 PostgreSQL schema.

Declared once here and migrated by Alembic (``migrations/versions``).
Rules this module follows:

- money columns are NUMERIC(12, 2) and Python keeps using ``Decimal``;
- status columns are plain strings with CHECK constraints over the exact
  enum values of :mod:`app.domain.enums` — no native PG enums, so the
  check text is derived from the enums and stays in sync by construction;
- JSON columns store JSONB on PostgreSQL and plain JSON elsewhere
  (``with_variant``), so the same metadata compiles on sqlite in tests;
- every CHECK / UNIQUE / FK / index is named explicitly, and
  ``migrations/versions/0001_initial.py`` creates the identical DDL
  (verified by compiling both sides to SQL and diffing them);
- no column exists here that has no counterpart on a domain model: this
  schema persists the domain, it does not extend it. ``refundable_amount``
  stays a derived property and is deliberately not stored.
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AgentRunStatus,
    ApprovalStatus,
    IssueType,
    OrderStatus,
    RefundStatus,
    RiskLevel,
    TicketStatus,
)
from app.persistence.base import Base

MONEY = sa.Numeric(12, 2)
TIMESTAMP = sa.DateTime(timezone=True)


def _enum_check(name: str, column: str, enum_type) -> sa.CheckConstraint:
    """CHECK that ``column`` holds one of the enum's exact values."""
    values = ", ".join(f"'{member.value}'" for member in enum_type)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


def _jsonb() -> sa.JSON:
    """JSONB on PostgreSQL, plain JSON elsewhere (sqlite compiles too)."""
    return sa.JSON().with_variant(JSONB(), "postgresql")


class OrderRow(Base):
    """Persisted order. ``refundable_amount`` is derived, never stored."""

    __tablename__ = "orders"
    __table_args__ = (
        _enum_check("ck_orders_status", "status", OrderStatus),
        sa.CheckConstraint("total_amount >= 0", name="ck_orders_total_nonneg"),
        sa.CheckConstraint("refunded_amount >= 0", name="ck_orders_refunded_nonneg"),
        sa.CheckConstraint("reserved_amount >= 0", name="ck_orders_reserved_nonneg"),
        # Order balance invariant: confirmed + reserved can never exceed
        # the total. Under concurrency this is kept true by the SQL store's
        # conditional reservation UPDATE; this CHECK is the last line of
        # defense against any path that bypasses it.
        sa.CheckConstraint(
            "refunded_amount + reserved_amount <= total_amount",
            name="ck_orders_balance_invariant",
        ),
    )

    order_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    refunded_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=sa.text("0")
    )
    reserved_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)


class TicketRow(Base):
    """Persisted support ticket bound to an order."""

    __tablename__ = "tickets"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name="fk_tickets_order_id"
        ),
        _enum_check("ck_tickets_status", "status", TicketStatus),
        _enum_check("ck_tickets_issue_type", "issue_type", IssueType),
        _enum_check("ck_tickets_risk_level", "risk_level", RiskLevel),
        sa.CheckConstraint(
            "claimed_amount IS NULL OR claimed_amount > 0",
            name="ck_tickets_claimed_positive",
        ),
        sa.Index("ix_tickets_order_id", "order_id"),
    )

    ticket_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    customer_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    issue_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    risk_level: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    claimed_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)


class RefundRow(Base):
    """Persisted (simulated) refund."""

    __tablename__ = "refunds"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_refunds_ticket_id"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name="fk_refunds_order_id"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.plan_id"], name="fk_refunds_plan_id"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),
        _enum_check("ck_refunds_status", "status", RefundStatus),
        _enum_check("ck_refunds_issue_type", "issue_type", IssueType),
        sa.CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        sa.CheckConstraint(
            "processed_at IS NULL OR processed_at >= created_at",
            name="ck_refunds_processed_after_created",
        ),
        sa.Index("ix_refunds_order_id", "order_id"),
        sa.Index("ix_refunds_ticket_status", "ticket_id", "status"),
        # At most one unresolved refund per order: PENDING (executing right
        # now) or UNKNOWN (must be resolved first). The gate the in-memory
        # store enforces under a lock becomes a database constraint here.
        sa.Index(
            "uq_refunds_order_unresolved",
            "order_id",
            unique=True,
            postgresql_where=sa.text("status IN ('pending', 'unknown')"),
            sqlite_where=sa.text("status IN ('pending', 'unknown')"),
        ),
    )

    refund_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    ticket_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    issue_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    plan_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # run_id is traceability only, deliberately NOT a foreign key: a
    # same-key retry from a later run must resolve the original record,
    # and that later run's row may not exist yet when it does.
    run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)


class ApprovalRow(Base):
    """Persisted human-approval record for a high-risk plan."""

    __tablename__ = "approvals"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_approvals_ticket_id"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.plan_id"], name="fk_approvals_plan_id"
        ),
        # order_id is deliberately NOT a foreign key: ApprovalService falls
        # back to plan.content.order_id or plan.ticket_id for legacy Day 2
        # fixtures, and the plan (which IS foreign-keyed) is what binds the
        # approved order at execution time.
        _enum_check("ck_approvals_status", "status", ApprovalStatus),
        _enum_check("ck_approvals_risk_level", "risk_level", RiskLevel),
        sa.CheckConstraint(
            "amount IS NULL OR amount > 0", name="ck_approvals_amount_positive"
        ),
        sa.CheckConstraint(
            "decided_at IS NULL OR decided_at >= requested_at",
            name="ck_approvals_decided_after_requested",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at >= requested_at",
            name="ck_approvals_expires_after_requested",
        ),
        sa.Index(
            "ix_approvals_ticket_requested",
            "ticket_id",
            sa.text("requested_at DESC"),
        ),
        # At most one PENDING approval per ticket and plan version; a new
        # request creates a new plan, so this blocks duplicate pending
        # requests without constraining decided history.
        sa.Index(
            "uq_approvals_pending_plan",
            "ticket_id",
            "plan_id",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        ),
    )

    approval_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    plan_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    risk_level: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    # NOT NULL: every persisted approval must bind the exact plan content
    # the human reviewed (the nullable domain field only serves legacy
    # Day 2 in-memory fixtures that never touch this table).
    plan_content_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    justification: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)


class PlanRow(Base):
    """Persisted resolution plan: content and its SHA-256 hash."""

    __tablename__ = "plans"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_plans_ticket_id"
        ),
        sa.Index("ix_plans_ticket_id", "ticket_id"),
    )

    plan_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Full ResolutionPlan content (model_dump(mode="json")).
    content: Mapped[dict] = mapped_column(_jsonb(), nullable=False)
    # SHA-256 hex digest of the canonical content serialization.
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)


class AuditEventRow(Base):
    """Append-only audit trail entry. Written once, never updated."""

    __tablename__ = "audit_events"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_audit_ticket_id"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name="fk_audit_order_id"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.run_id"], name="fk_audit_run_id"
        ),
        sa.CheckConstraint(
            "actor IN ('system', 'agent', 'human')", name="ck_audit_actor"
        ),
        sa.Index("ix_audit_ticket_ts", "ticket_id", "timestamp"),
        sa.Index("ix_audit_run", "run_id"),
    )

    event_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    ticket_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    details: Mapped[dict] = mapped_column(_jsonb(), nullable=False)


class AgentRunRow(Base):
    """Persisted bookkeeping for one agent run."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_runs_ticket_id"
        ),
        _enum_check("ck_runs_status", "status", AgentRunStatus),
        sa.CheckConstraint("iterations >= 0", name="ck_runs_iterations_nonneg"),
        sa.CheckConstraint(
            "max_iterations > 0", name="ck_runs_max_iterations_positive"
        ),
        sa.CheckConstraint(
            "iterations <= max_iterations", name="ck_runs_iterations_capped"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_runs_finished_after_started",
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'failed', 'cancelled', 'awaiting_approval')"
            " OR finished_at IS NOT NULL",
            name="ck_runs_terminal_finished",
        ),
        sa.CheckConstraint(
            "status = 'failed' OR error IS NULL", name="ck_runs_error_only_failed"
        ),
        sa.Index("ix_agent_runs_ticket_id", "ticket_id"),
    )

    run_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    iterations: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    max_iterations: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("10")
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class TicketNoteRow(Base):
    """Persisted free-text note appended to a ticket."""

    __tablename__ = "ticket_notes"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_notes_ticket_id"
        ),
        sa.CheckConstraint(
            "author IN ('agent', 'human')", name="ck_notes_author"
        ),
        sa.Index("ix_ticket_notes_ticket_id", "ticket_id"),
    )

    note_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    author: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    text: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
