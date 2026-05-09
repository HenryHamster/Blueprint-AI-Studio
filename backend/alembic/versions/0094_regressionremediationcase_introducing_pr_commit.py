"""Add introducing_pr_number and introducing_commit_sha to RegressionRemediationCase.

Records which PR / commit most plausibly introduced a viewport (or other)
regression so operators can triage without manual git-blame and the
autonomous loop can credit/blame a PR for a recurring case.

Revision ID: 0094
Revises: 0093
Create Date: 2026-05-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("introducing_pr_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "regressionremediationcase",
        sa.Column("introducing_commit_sha", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "introducing_commit_sha")
    op.drop_column("regressionremediationcase", "introducing_pr_number")
