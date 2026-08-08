"""Record clean source identity for self-hosted fleet runners.

LocalRunner.version remains the human-readable git-describe value. The nullable
source_sha column carries the separately validated commit identity used by the
release serving gate.

Revision ID: 0739
Revises: 0738
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0739"
down_revision = "0738"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "localrunner",
        sa.Column("source_sha", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("localrunner", "source_sha")
