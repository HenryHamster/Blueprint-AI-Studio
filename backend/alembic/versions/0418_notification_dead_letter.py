"""notification_dead_letter

Create the notification_dead_letter table to durably record external
notifications that failed to deliver — either because they exhausted their
retries or were evicted from the full in-memory retry queue. This replaces the
silent in-memory loss with an operator-visible, re-drivable record.

Revision ID: 0418
Revises: 0414
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0418"
down_revision = "0414"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_dead_letter",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("webhook_url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("body", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_dead_letter_project_id",
        "notification_dead_letter",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_dead_letter_project_id",
        table_name="notification_dead_letter",
    )
    op.drop_table("notification_dead_letter")
