"""Add composite index on task (project_id, status, updated_at).

Queries in strategic_spawning.py, recovery_sweeps.py, and reaper.py all
ORDER BY Task.updated_at with WHERE filters on project_id + status. The
existing ix_task_project_status_created covers (project_id, status, created_at)
but not updated_at, forcing an in-memory filesort for every such call.

Created CONCURRENTLY on Postgres so production rollouts don't block writes.
CONCURRENTLY cannot run inside a transaction, so we issue COMMIT first to
release Alembic's wrapping transaction. IF NOT EXISTS makes the migration
idempotent. The SQLite path uses a regular op.create_index so the invariant
test (test_model_indexes_covered_by_migrations) can detect the migration.

Revision ID: 0237
Revises: 0236
"""

from alembic import op


revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_project_status_updated "
            "ON task (project_id, status, updated_at)"
        )
    else:
        op.create_index(
            "ix_task_project_status_updated",
            "task",
            ["project_id", "status", "updated_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_project_status_updated")
    else:
        op.drop_index("ix_task_project_status_updated", table_name="task")
