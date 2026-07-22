"""Add delivered_at column to projectmessage for WS delivery observability.

The delivered_at timestamp is set when a WebSocket client acknowledges
receiving the broadcast frame via a message_ack. NULL delivered_at on a
message that has been broadcast IS the durable diagnostic record of a
delivery gap — used by the recovery-on-reconnect path to re-send
undelivered messages and by operator_inbox_liveness to detect silent drops.

Revision ID: 0670
Revises: 0669
Create Date: 2026-07-22 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0670"
down_revision = "0669"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectmessage",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projectmessage", "delivered_at")
