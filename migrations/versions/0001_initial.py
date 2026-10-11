"""Initial schema: the eight SupportFlow tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-10

Self-contained by design: every table, constraint and index below mirrors
``app.persistence.models`` exactly (same names, types and check text), and
is verified against it by compiling both sides to SQL and diffing them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 2)
TIMESTAMP = sa.DateTime(timezone=True)
JSONB_TEXT = sa.JSON().with_variant(JSONB(), "postgresql")

# Status CHECK texts: the exact enum values of app.domain.enums, frozen
# here so a later enum change cannot silently rewrite this migration.
ORDER_STATUS = "'pending', 'paid', 'shipped', 'delivered', 'cancelled'"
TICKET_STATUS = "'open', 'in_progress', 'pending_approval', 'resolved', 'closed', 'cancelled'"
ISSUE_TYPE = "'refund_request', 'order_inquiry', 'delivery_issue', 'damaged_item', 'other'"
RISK_LEVEL = "'low', 'medium', 'high', 'critical'"
REFUND_STATUS = "'pending', 'approved', 'rejected', 'processed', 'failed', 'unknown'"
APPROVAL_STATUS = "'pending', 'approved', 'rejected', 'expired'"
RUN_STATUS = "'running', 'awaiting_approval', 'completed', 'failed', 'cancelled'"


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_amount", MONEY, nullable=False),
        sa.Column(
            "refunded_amount", MONEY, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "reserved_amount", MONEY, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("delivered_at", TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            f"status IN ({ORDER_STATUS})", name="ck_orders_status"
        ),
        sa.CheckConstraint("total_amount >= 0", name="ck_orders_total_nonneg"),
        sa.CheckConstraint(
            "refunded_amount >= 0", name="ck_orders_refunded_nonneg"
        ),
        sa.CheckConstraint(
            "reserved_amount >= 0", name="ck_orders_reserved_nonneg"
        ),
        sa.CheckConstraint(
            "refunded_amount + reserved_amount <= total_amount",
            name="ck_orders_balance_invariant",
        ),
    )

    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.String(64), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("issue_type", sa.String(24), nullable=False),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("claimed_amount", MONEY, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name="fk_tickets_order_id"
        ),
        sa.CheckConstraint(f"status IN ({TICKET_STATUS})", name="ck_tickets_status"),
        sa.CheckConstraint(
            f"issue_type IN ({ISSUE_TYPE})", name="ck_tickets_issue_type"
        ),
        sa.CheckConstraint(
            f"risk_level IN ({RISK_LEVEL})", name="ck_tickets_risk_level"
        ),
        sa.CheckConstraint(
            "claimed_amount IS NULL OR claimed_amount > 0",
            name="ck_tickets_claimed_positive",
        ),
    )
    op.create_index("ix_tickets_order_id", "tickets", ["order_id"])

    op.create_table(
        "plans",
        sa.Column("plan_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("content", JSONB_TEXT, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_plans_ticket_id"
        ),
    )
    op.create_index("ix_plans_ticket_id", "plans", ["ticket_id"])

    op.create_table(
        "refunds",
        sa.Column("refund_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("issue_type", sa.String(24), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=True),
        # run_id is traceability only and deliberately has no FK: a
        # same-key retry from a later run must resolve the original record.
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("processed_at", TIMESTAMP, nullable=True),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_refunds_ticket_id"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name="fk_refunds_order_id"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.plan_id"], name="fk_refunds_plan_id"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_refunds_idempotency_key"
        ),
        sa.CheckConstraint(f"status IN ({REFUND_STATUS})", name="ck_refunds_status"),
        sa.CheckConstraint(
            f"issue_type IN ({ISSUE_TYPE})", name="ck_refunds_issue_type"
        ),
        sa.CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        sa.CheckConstraint(
            "processed_at IS NULL OR processed_at >= created_at",
            name="ck_refunds_processed_after_created",
        ),
    )
    op.create_index("ix_refunds_order_id", "refunds", ["order_id"])
    op.create_index("ix_refunds_ticket_status", "refunds", ["ticket_id", "status"])
    # At most one unresolved refund per order (PENDING or UNKNOWN) — the
    # same-order gate the in-memory store enforces under a lock.
    op.create_index(
        "uq_refunds_order_unresolved",
        "refunds",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'unknown')"),
        sqlite_where=sa.text("status IN ('pending', 'unknown')"),
    )

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        # Not a foreign key: ApprovalService falls back to
        # plan.content.order_id or plan.ticket_id for legacy fixtures.
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("amount", MONEY, nullable=True),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("plan_content_hash", sa.String(128), nullable=False),
        sa.Column("justification", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", TIMESTAMP, nullable=False),
        sa.Column("decided_at", TIMESTAMP, nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("expires_at", TIMESTAMP, nullable=True),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_approvals_ticket_id"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.plan_id"], name="fk_approvals_plan_id"
        ),
        sa.CheckConstraint(
            f"status IN ({APPROVAL_STATUS})", name="ck_approvals_status"
        ),
        sa.CheckConstraint(
            f"risk_level IN ({RISK_LEVEL})", name="ck_approvals_risk_level"
        ),
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
    )
    op.create_index(
        "ix_approvals_ticket_requested",
        "approvals",
        ["ticket_id", sa.text("requested_at DESC")],
    )
    # At most one PENDING approval per ticket and plan version.
    op.create_index(
        "uq_approvals_pending_plan",
        "approvals",
        ["ticket_id", "plan_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", TIMESTAMP, nullable=False),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column(
            "iterations", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "max_iterations", sa.Integer, nullable=False, server_default=sa.text("10")
        ),
        sa.Column("error", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_runs_ticket_id"
        ),
        sa.CheckConstraint(f"status IN ({RUN_STATUS})", name="ck_runs_status"),
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
    )
    op.create_index("ix_agent_runs_ticket_id", "agent_runs", ["ticket_id"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("timestamp", TIMESTAMP, nullable=False),
        sa.Column("actor", sa.String(8), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column("order_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("details", JSONB_TEXT, nullable=False),
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
    )
    op.create_index("ix_audit_ticket_ts", "audit_events", ["ticket_id", "timestamp"])
    op.create_index("ix_audit_run", "audit_events", ["run_id"])

    op.create_table(
        "ticket_notes",
        sa.Column("note_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("author", sa.String(8), nullable=False),
        sa.Column("text", sa.String(2000), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"], name="fk_notes_ticket_id"
        ),
        sa.CheckConstraint(
            "author IN ('agent', 'human')", name="ck_notes_author"
        ),
    )
    op.create_index("ix_ticket_notes_ticket_id", "ticket_notes", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_notes_ticket_id", table_name="ticket_notes")
    op.drop_table("ticket_notes")
    op.drop_index("ix_audit_run", table_name="audit_events")
    op.drop_index("ix_audit_ticket_ts", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_agent_runs_ticket_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("uq_approvals_pending_plan", table_name="approvals")
    op.drop_index("ix_approvals_ticket_requested", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("uq_refunds_order_unresolved", table_name="refunds")
    op.drop_index("ix_refunds_ticket_status", table_name="refunds")
    op.drop_index("ix_refunds_order_id", table_name="refunds")
    op.drop_table("refunds")
    op.drop_index("ix_plans_ticket_id", table_name="plans")
    op.drop_table("plans")
    op.drop_index("ix_tickets_order_id", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("orders")
