"""Add theme column to release_version for strategist-authored delivery shape.

Revision ID: 0332
Revises: 0331
"""

import sqlalchemy as sa
from alembic import op


revision = "0332"
down_revision = "0331"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column("theme", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "theme")
