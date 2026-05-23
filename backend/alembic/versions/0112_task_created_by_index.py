"""Add index on task.created_by column.

Revision ID: 0112
Revises: 0111
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_task_created_by", "task", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_task_created_by", table_name="task")
