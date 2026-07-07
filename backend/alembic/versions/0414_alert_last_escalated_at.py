"""alert_last_escalated_at

Add last_escalated_at column to alert table for dedicated escalation cadence
tracking in the periodic runner-deactivated re-page sweep, separate from
last_seen_at which alert_policy owns for dedup tracking.

Revision ID: 0414
Revises: 0413
"""

import sqlalchemy as sa
from alembic import op

revision = "0414"
down_revision = "0413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("last_escalated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alert", "last_escalated_at")
