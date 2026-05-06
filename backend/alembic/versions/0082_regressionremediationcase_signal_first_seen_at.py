"""Add signal_first_seen_at to regressionremediationcase for MTTD metric.

Records when the underlying signal first crossed threshold so callers can
compute time_to_detect = case.created_at - signal_first_seen_at.

Revision ID: 0082
Revises: 0081
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("signal_first_seen_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "signal_first_seen_at")
