"""ix_artifact_task_type

Add composite index on Artifact(task_id, artifact_type) to support
index-only scans for the common query pattern:
  WHERE task_id IN (...) AND artifact_type IN (...)

Used by the task-list logs query in admin.py and the activity endpoint
in admin_activity.py. PostgreSQL previously used the single task_id index
and then re-fetched heap pages to filter on artifact_type.

Revision ID: 0397
Revises: 0396
"""

from alembic import op


revision = "0397"
down_revision = "0396"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_artifact_task_type",
        "artifact",
        ["task_id", "artifact_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_task_type", table_name="artifact")
