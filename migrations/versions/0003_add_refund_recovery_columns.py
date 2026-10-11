"""Add the recovery lease and attempt-tracking columns to refunds.

Day 6 adds a recovery service that re-discovers UNKNOWN (and orphaned
PENDING) refunds after an application restart and settles them from
gateway status queries. The four columns support the minimal claim
mechanism and its observability:

- ``recovery_claim_owner`` / ``recovery_claim_expires_at``: the lease. A
  worker claims a refund with one atomic conditional UPDATE, queries the
  gateway outside any transaction, and must still own the lease (owner
  token match) before its settle UPDATE is applied. Expiry only means
  "another worker may query again" — it never means "execute again".
- ``recovery_attempts``: how many times the refund has been claimed for
  recovery (bounded retry accounting, visible to operators).
- ``last_recovery_error``: the latest query error / unknown-reason, so a
  repeatedly-unresolvable refund shows *why* it is stuck.

The refund id itself is the stable gateway query key (persisted on the
row before any execute call), so no extra gateway-reference column is
needed.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "refunds",
        sa.Column("recovery_claim_owner", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "refunds",
        sa.Column("recovery_claim_expires_at", TIMESTAMP, nullable=True),
    )
    op.add_column(
        "refunds",
        sa.Column(
            "recovery_attempts",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "refunds",
        sa.Column("last_recovery_error", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("refunds", "last_recovery_error")
    op.drop_column("refunds", "recovery_attempts")
    op.drop_column("refunds", "recovery_claim_expires_at")
    op.drop_column("refunds", "recovery_claim_owner")
