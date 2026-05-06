"""Add last_failure_class column to RegressionRemediationCase.

Captures the classify_failure() result of the most recent failure on a case
so the regressions panel can surface the failure class without recomputing
it from the underlying task. Backfill is implicit NULL for existing rows.

Revision ID: 0077
Revises: 0076
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("last_failure_class", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "last_failure_class")
