"""projectmessage_from_task_id_index

Add index on ProjectMessage.from_task_id to avoid full sequential scan
when querying messages by task (e.g., admin_agent_ops task-message lookup).

Revision ID: 0382
Revises: 0381
"""

from alembic import op


revision = "0382"
down_revision = "0381"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_projectmessage_from_task_id", "projectmessage", ["from_task_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_projectmessage_from_task_id", table_name="projectmessage")
