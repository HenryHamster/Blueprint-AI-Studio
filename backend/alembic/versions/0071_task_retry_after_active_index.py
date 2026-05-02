"""Partial expression index on retry_after_until for the reaper sweep.

Round 5 audit fix R5-W2-C: the reaper's stale ``retry_after`` sweep
filters ``Task.status == QUEUED AND Task.retry_after_until IS NOT NULL``
every reaper tick. None of the existing composite indexes match that
exact partial predicate — ``ix_task_status_priority_created`` covers the
status leading column but not the retry_after_until refinement, and the
composite reaper indexes (``ix_task_status_last_heartbeat``,
``ix_task_parent_task_id_status``) are partial on different status sets.
At 1k users with a growing QUEUED queue the LIMIT-5000 sweep degrades to
linear-scan cost.

This migration adds a partial expression index on
``retry_after_until`` filtered to QUEUED rows whose retry hold is set,
matching the sweep's filter shape directly. Postgres-only — SQLite test
fixtures don't accumulate the QUEUED queue depth that motivates the
index, but the SQLite-fallback ``op.create_index`` is still emitted so
the ``test_model_indexes_covered_by_migrations`` invariant passes.

Created CONCURRENTLY on Postgres so production rollouts don't block
writes on the ``task`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first to release Alembic's wrapping
transaction, then issue the DDL on the now-autocommit connection.

Revision ID: 0069
Revises: 0068
Create Date: 2026-04-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Postgres source-of-truth: partial index matching the reaper's
        # filter shape exactly. CONCURRENTLY requires no surrounding
        # transaction; commit alembic's wrapper first.
        op.execute("COMMIT")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_retry_after_active
            ON task (retry_after_until)
            WHERE status = 'QUEUED' AND retry_after_until IS NOT NULL
            """
        )
    else:
        # SQLite test path: register the index name + column reference so
        # ``test_model_indexes_covered_by_migrations`` sees the metadata
        # Index in models.py is satisfied. The partial WHERE is faithfully
        # carried via sqlite_where, mirroring 0057's pattern.
        op.create_index(
            "ix_task_retry_after_active",
            "task",
            ["retry_after_until"],
            sqlite_where=sa.text(
                "status = 'QUEUED' AND retry_after_until IS NOT NULL"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_retry_after_active")
    else:
        op.drop_index("ix_task_retry_after_active", table_name="task")
