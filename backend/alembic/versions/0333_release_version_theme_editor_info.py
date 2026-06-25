"""Add theme_editor_info column to release_version for operator-freeze guard.

Revision ID: 0333
Revises: 0332
"""

import sqlalchemy as sa
from alembic import op


revision = "0333"
down_revision = "0332"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column("theme_editor_info", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "theme_editor_info")
