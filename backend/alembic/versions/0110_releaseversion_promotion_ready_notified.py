"""Add promotion_ready_notified to releaseversion.

Durable deduplication anchor for promotion-readiness alerting.
Set to True when a promotion_readiness_reached notification fires for the
current ready window; reset to False when readiness lapses so the next
not-ready→ready transition can notify again.

Nullable=False, server_default='false': existing rows default to unnotified.

Revision ID: 0110
Revises: 0109
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "promotion_ready_notified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "promotion_ready_notified")
