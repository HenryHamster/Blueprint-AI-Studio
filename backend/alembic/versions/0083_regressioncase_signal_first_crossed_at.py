"""Add signal_first_crossed_at to regressionremediationcase for time-to-detect metric.

Revision ID: 0083
Revises: 0082
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("signal_first_crossed_at", sa.DateTime(timezone=False), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "signal_first_crossed_at")
