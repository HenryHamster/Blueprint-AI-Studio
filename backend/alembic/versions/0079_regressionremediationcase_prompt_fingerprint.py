"""Add prompt_fingerprint_at_spawn to regressionremediationcase.

Records which prompt fingerprint was active when a regression was first
spawned for remediation — enables correlating regression onset/recovery
timing with prompt-version changes rather than requiring manual git bisect.

Revision ID: 0079
Revises: 0078
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column("prompt_fingerprint_at_spawn", sa.String(length=12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "prompt_fingerprint_at_spawn")
