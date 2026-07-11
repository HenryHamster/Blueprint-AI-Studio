"""Add partial (status, completed_at) index for global COMPLETED scans.

The acceptance-gate backfill query (acceptance_gate.py:1170-1175) scans for
the 200 oldest COMPLETED tasks without a project_id filter.  The existing
ix_task_project_status_composite has project_id as the leading column, so it
cannot serve a project-less ORDER BY completed_at ASC scan.  This partial index
on (status, completed_at) WHERE status = 'COMPLETED' covers that query with a
tight index range scan.

Revision ID: 0489
Revises: 0488
Create Date: 2026-07-11

"""

from alembic import op

revision = "0489"
down_revision = "0488"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_status_completed_at "
            "ON task (status, completed_at) WHERE status = 'COMPLETED'"
        )
    else:
        # SQLite test environments — plain index without partial predicate.
        # The test_model_indexes_covered_by_migrations scanner needs an explicit
        # op.create_index() call to register the index as migration-covered.
        op.create_index(
            "ix_task_status_completed_at", "task", ["status", "completed_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_status_completed_at")
    else:
        op.drop_index("ix_task_status_completed_at", table_name="task")
