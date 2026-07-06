"""ix_task_vm_stopped_at

Add partial index on Task.vm_stopped_at for anomaly_detection range
filter + ORDER BY queries (WHERE vm_stopped_at >= cutoff ORDER BY vm_stopped_at).
Without this index the query full-scans the task table on every anomaly sweep.

Revision ID: 0383
Revises: 0382
"""

from alembic import op
from sqlalchemy import text


revision = "0383"
down_revision = "0382"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if the index already exists (it may have been created by 0381)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        result = bind.execute(
            text(
                """SELECT 1 FROM pg_indexes
                   WHERE indexname = 'ix_task_vm_stopped_at'"""
            )
        )
        if result.fetchone():
            return

    op.create_index(
        "ix_task_vm_stopped_at",
        "task",
        ["vm_stopped_at"],
        postgresql_where=text("vm_stopped_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_vm_stopped_at", table_name="task")
