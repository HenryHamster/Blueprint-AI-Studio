"""Add Project.integration_branch column for per-project trunk branch config.

Backfills 'automain' for the self-repo project (is_self_repo=True)
and leaves all other rows NULL (falls back to runtime_config then 'main').

Revision ID: 0124
Revises: 0123
Create Date: 2026-05-23 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("integration_branch", sa.String(), nullable=True),
    )
    # Backfill: self-repo projects use 'automain'; others stay NULL (→ 'main' fallback).
    op.execute(
        "UPDATE project SET integration_branch = 'automain' WHERE is_self_repo = TRUE"
    )


def downgrade() -> None:
    op.drop_column("project", "integration_branch")
