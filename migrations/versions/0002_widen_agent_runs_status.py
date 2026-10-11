"""Widen agent_runs.status to fit 'awaiting_approval' (17 chars).

Day 5 wired the agent graph onto PostgreSQL and the first approval-gate
run failed with ``value too long for type character varying(16)``: the
original 16-char width matched the other status columns, but
``AgentRunStatus.AWAITING_APPROVAL`` is one character longer than every
other status value. Day 4's integration tests never persisted that
status, so the width bug was only visible once the full agent flow ran
on the real backend.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "agent_runs",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "agent_runs",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
