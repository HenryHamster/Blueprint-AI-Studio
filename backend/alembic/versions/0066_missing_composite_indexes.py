"""Add composite indexes for reaper + claim hot paths.

Audit 2026-04-29 (DB comprehensive remediation, Phase E1 / findings M-P2,
M-P3):

Two composite indexes that the reaper sweep and the parent-children
lookup hot paths actually use are currently missing, forcing sequential
scans on the ``task`` table at scale:

1. ``ix_task_status_last_heartbeat`` — ``(status, last_heartbeat)``
   partial WHERE ``status IN ('IN_PROGRESS', 'PROVISIONING')``. Used by
   the reaper's stale-heartbeat scan (``reaper.py``) which selects the
   active-but-quiet rows by status and orders/filters by
   ``last_heartbeat``. Partial keeps the index small (terminal rows
   excluded) while still covering every active row the reaper visits.

2. ``ix_task_parent_task_id_status`` — ``(parent_task_id, status)``
   partial WHERE ``parent_task_id IS NOT NULL``. Used by rollup,
   reaper, and services for parent-children lookups (e.g.
   ``Task.parent_task_id == :pid AND Task.status IN (...)``). Partial
   excludes top-level rows with NULL parent so the index only contains
   actual children.

Both are created CONCURRENTLY on Postgres so production rollouts don't
block writes on the ``task`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first to release Alembic's wrapping
transaction, then issue the DDL on the now-autocommit connection. ``IF
NOT EXISTS`` makes the migration idempotent across re-runs.

The SQLite branch creates regular B-tree composites without partial
predicates so the test invariant
(``test_model_indexes_covered_by_migrations``) sees the index name +
columns. The Postgres path is the source of truth for prod.

Revision ID: 0064
Revises: 0063
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Postgres source-of-truth path: real partial composite indexes,
        # created CONCURRENTLY so writers aren't blocked on a long build.
        # CONCURRENTLY cannot run inside a transaction, so we commit
        # Alembic's wrapping transaction first. IF NOT EXISTS makes the
        # migration idempotent across re-runs and across environments
        # where the index may have been hand-created.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_status_last_heartbeat "
            "ON task (status, last_heartbeat) "
            "WHERE status IN ('IN_PROGRESS', 'PROVISIONING')"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_parent_task_id_status "
            "ON task (parent_task_id, status) "
            "WHERE parent_task_id IS NOT NULL"
        )
    else:
        # SQLite (test path): create regular B-tree composites so the
        # index names + columns are registered against metadata. Static
        # analysis of this migration (test_model_indexes_covered_by_migrations)
        # uses these op.create_index calls to verify the metadata Index
        # entries in models.py have corresponding migration entries.
        op.create_index(
            "ix_task_status_last_heartbeat",
            "task",
            ["status", "last_heartbeat"],
        )
        op.create_index(
            "ix_task_parent_task_id_status",
            "task",
            ["parent_task_id", "status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_status_last_heartbeat")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_parent_task_id_status")
    else:
        op.drop_index("ix_task_status_last_heartbeat", table_name="task")
        op.drop_index("ix_task_parent_task_id_status", table_name="task")
