"""Add composite indexes for TaskHistory and NotificationDeadLetter.

BR-14141 / g-db-perf-composite-indexes

Following a performance audit finding:
- ix_taskhistory_task_id_timestamp_desc: Covers the common query pattern
  WHERE task_id IN (...) ORDER BY timestamp DESC LIMIT N — avoids an
  in-memory sort of all matching rows per task_id.
- ix_notification_dl_project_id_created_at_desc: Covers the common query
  pattern WHERE project_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?
  on the append-only dead-letter table.

Revision ID: 0487
Revises: 0486
Create Date: 2026-07-11 04:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0487"
down_revision = "0486"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_taskhistory_task_id_timestamp_desc",
        "taskhistory",
        ["task_id", sa.text("timestamp desc")],
    )
    op.create_index(
        "ix_notification_dl_project_id_created_at_desc",
        "notification_dead_letter",
        ["project_id", sa.text("created_at desc")],
    )


def downgrade():
    op.drop_index(
        "ix_notification_dl_project_id_created_at_desc",
        table_name="notification_dead_letter",
    )
    op.drop_index(
        "ix_taskhistory_task_id_timestamp_desc",
        table_name="taskhistory",
    )
