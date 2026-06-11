"""Add lane-related index coverage.

Revision ID: 0234
Revises: 0233
"""

from alembic import op


revision = "0234"
down_revision = "0233"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_improvementlane_project_id",
        "improvementlane",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_improvementlane_direction_id",
        "improvementlane",
        ["direction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_improvementlane_direction_id", table_name="improvementlane")
    op.drop_index("ix_improvementlane_project_id", table_name="improvementlane")
