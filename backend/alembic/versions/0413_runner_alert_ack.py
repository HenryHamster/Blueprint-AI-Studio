"""runner_alert_ack

Add acknowledged_at column to alert table for operator acknowledgement of
ongoing runner outage alerts, suppressing further escalation re-pages until
the runner recovers.

Revision ID: 0413
Revises: 0411
"""

import sqlalchemy as sa
from alembic import op

revision = "0413"
down_revision = "0411"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alert", "acknowledged_at")
