"""Add Project.promotion_target column for per-project promotion branch config.

NULL means fall back to runtime_config["promotion_target"] then "main" —
identical semantics to integration_branch. No backfill needed.

Revision ID: 0125
Revises: 0124
Create Date: 2026-05-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("promotion_target", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project", "promotion_target")
