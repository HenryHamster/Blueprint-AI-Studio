"""Add composite index on strategiccycle(project_id, created_at).

Revision ID: 0121
Revises: 0120
Create Date: 2026-05-23 00:00:00.000000

"""

from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_strategiccycle_project_created",
        "strategiccycle",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategiccycle_project_created", table_name="strategiccycle")
