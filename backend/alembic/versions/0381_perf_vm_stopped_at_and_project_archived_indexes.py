"""perf_vm_stopped_at_and_project_archived_indexes

Add partial index on Task.vm_stopped_at (for anomaly_detection cost-window
range queries) and a plain index on Project.archived (filtered on every
project listing query).

Revision ID: 0381
Revises: 0380
"""

from alembic import op


revision = "0381"
down_revision = "0380"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_task_vm_stopped_at",
        "task",
        ["vm_stopped_at"],
        postgresql_where="vm_stopped_at IS NOT NULL",
    )
    op.create_index("ix_project_archived", "project", ["archived"])


def downgrade() -> None:
    op.drop_index("ix_task_vm_stopped_at", table_name="task")
    op.drop_index("ix_project_archived", table_name="project")
