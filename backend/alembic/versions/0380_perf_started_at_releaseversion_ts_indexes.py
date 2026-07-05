"""perf_started_at_releaseversion_ts_indexes

Add index on Task.started_at (used by active-task digest ORDER BY) and
composite indexes on ReleaseVersion timestamp columns (project_id + opened_at /
served_at / promoted_at) to eliminate filesorts on the 9 hot admin query sites.

Revision ID: 0380
Revises: 0379
"""

from alembic import op


revision = "0380"
down_revision = "0379"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_task_started_at", "task", ["started_at"])
    op.create_index(
        "ix_releaseversion_project_opened_at",
        "releaseversion",
        ["project_id", "opened_at"],
    )
    op.create_index(
        "ix_releaseversion_project_served_at",
        "releaseversion",
        ["project_id", "served_at"],
    )
    op.create_index(
        "ix_releaseversion_project_promoted_at",
        "releaseversion",
        ["project_id", "promoted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_started_at", table_name="task")
    op.drop_index("ix_releaseversion_project_opened_at", table_name="releaseversion")
    op.drop_index("ix_releaseversion_project_served_at", table_name="releaseversion")
    op.drop_index("ix_releaseversion_project_promoted_at", table_name="releaseversion")
