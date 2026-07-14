"""Add index on Artifact.created_at to speed ORDER BY queries.

Revision ID: 0492
Revises: 0468
Create Date: 2026-07-11 00:00:00.000000

"""

from alembic import op

revision = "0492"
down_revision = "0489"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_artifact_created_at", "artifact", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_artifact_created_at", table_name="artifact")
