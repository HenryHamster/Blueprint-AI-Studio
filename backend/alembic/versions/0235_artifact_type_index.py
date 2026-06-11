"""Add B-tree index on artifact.artifact_type.

Revision ID: 0235
Revises: 0234
"""

from alembic import op


revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_artifact_artifact_type", "artifact", ["artifact_type"])


def downgrade() -> None:
    op.drop_index("ix_artifact_artifact_type", table_name="artifact")
