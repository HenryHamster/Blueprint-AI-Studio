"""Add last_unactionable_verdict and last_unactionable_summary to RegressionRemediationCase.

Revision ID: 0123
Revises: 0122
Create Date: 2026-05-23 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("last_unactionable_verdict", sa.String(64), nullable=True),
    )
    op.add_column(
        "regressionremediationcase",
        sa.Column("last_unactionable_summary", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "last_unactionable_summary")
    op.drop_column("regressionremediationcase", "last_unactionable_verdict")
