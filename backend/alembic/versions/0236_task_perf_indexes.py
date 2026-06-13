"""Add performance indexes for updated_at, completed_at, and terminal last_heartbeat.

Three composite indexes that hot query paths needed but were missing:

1. ``ix_task_project_status_updated`` — ``(project_id, status, updated_at)``.
   strategic_spawning.py queries COMPLETED tasks ordered by updated_at DESC
   (with optional updated_at range filter). The existing
   ix_task_project_status_created serves the equality predicates but forces
   a filesort on updated_at.

2. ``ix_task_project_status_completed`` — ``(project_id, status, completed_at)``.
   review_loop_convergence.py queries COMPLETED/EVALUATING tasks ordered by
   completed_at DESC with no LIMIT. The existing index has created_at in the
   trailing position, so completed_at forces a filesort.

3. ``ix_task_terminal_last_heartbeat`` — partial on ``(last_heartbeat)``
   WHERE status IN ('COMPLETED','FAILED') AND vm_instance_id IS NOT NULL.
   admin_health.py health-overview endpoint filters by these exact predicates
   and orders by last_heartbeat DESC. The existing ix_task_status_last_heartbeat
   is partial on IN_PROGRESS/PROVISIONING so its predicate actively excludes
   these rows.

All three are created CONCURRENTLY on Postgres so production rollouts don't
block writes on the ``task`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first. ``IF NOT EXISTS`` makes the
migration idempotent. SQLite branch uses regular B-tree indexes so the
test_model_indexes_covered_by_migrations invariant sees the index names.

Revision ID: 0236
Revises: 0235
"""

from alembic import op


revision = "0236"
down_revision = "0235"
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
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_project_status_completed "
            "ON task (project_id, status, completed_at)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_terminal_last_heartbeat "
            "ON task (last_heartbeat) "
            "WHERE status IN ('COMPLETED', 'FAILED') AND vm_instance_id IS NOT NULL"
        )
    else:
        op.create_index(
            "ix_task_project_status_updated",
            "task",
            ["project_id", "status", "updated_at"],
        )
        op.create_index(
            "ix_task_project_status_completed",
            "task",
            ["project_id", "status", "completed_at"],
        )
        op.create_index(
            "ix_task_terminal_last_heartbeat",
            "task",
            ["last_heartbeat"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_project_status_updated")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_project_status_completed")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_terminal_last_heartbeat")
    else:
        op.drop_index("ix_task_project_status_updated", table_name="task")
        op.drop_index("ix_task_project_status_completed", table_name="task")
        op.drop_index("ix_task_terminal_last_heartbeat", table_name="task")
