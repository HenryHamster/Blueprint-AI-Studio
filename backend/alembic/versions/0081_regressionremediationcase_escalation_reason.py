"""Add escalation_reason to regressionremediationcase.

Distinguishes the path that drove a case into NEEDS_MANUAL_INTERVENTION:
'attempts_exhausted', 'investigations_diverged', 'stale_active', 'stale_merged'.
Stored as a free-form String (not a SQL enum) so adding new reasons in the
future is a code-only change.

Revision ID: 0081
Revises: 0080
Create Date: 2026-05-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("escalation_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "escalation_reason")
