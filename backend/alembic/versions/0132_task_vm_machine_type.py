"""Add task VM machine type.

Revision ID: 0132
Revises: 0131
Create Date: 2026-06-01 07:45:00

"""

from alembic import op
import sqlalchemy as sa


revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("vm_machine_type", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "vm_machine_type")
