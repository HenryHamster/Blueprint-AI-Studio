"""Add partial index on task.requires_approval for digest OR-filter optimization.

The digest precompute query used:
  created_at >= lookback AND (status IN (COMPLETED,FAILED,CANCELLED) OR requires_approval IS TRUE)

This OR clause prevents any composite index from being used, forcing a BitmapOr or
seq-scan over the time-windowed slice for the requires_approval half.

The partial index (WHERE requires_approval IS TRUE) lets the split query serve the
approval-task half via an index-only scan, avoiding the BitmapOr penalty.

Revision ID: 0275
Revises: 0274
"""

from alembic import op

revision = "0275"
down_revision = "0274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_requires_approval "
        "ON task (project_id, created_at) WHERE requires_approval IS TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_task_requires_approval")
