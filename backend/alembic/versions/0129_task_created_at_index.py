"""Add leading-created_at index on task for global admin-analytics scans.

The digest (``admin_digest._compute_digest``) and health-overview
(``admin_health._compute_health_overview``) endpoints, when called without a
``project_id`` (the global admin view), filter ``task.created_at >= window``
over all projects. Every existing composite index has ``created_at`` as a
TRAILING column (``ix_task_status_priority_created``,
``ix_task_project_status_created``, ``ix_task_provider_created_at``, etc.), so
the global time-window scan had no usable leading index and full-scanned the
whole ``task`` table. As the table grew this crossed the 120s
``statement_timeout`` — incident 2026-05-29: a digest-query timeout storm
(~296/30min) that 500-ed the dashboard, starved the shared connection pool, and
stalled the orchestrator main cycle ("no progress heartbeat in 198s" hang). A
plain leading-``created_at`` btree lets the planner range-scan just the recent
window instead of the whole table.

Built CONCURRENTLY on PostgreSQL (matching the other task index migrations,
e.g. 0113/0115/0116/0128) so the build never blocks task writes and is not
itself subject to a statement timeout on a large table. The SQLite branch uses
a plain ``op.create_index`` with literal args — also what the model↔migration
index-parity invariant (test_invariants.test_model_indexes_covered_by_migrations)
scans for.

Revision ID: 0129
Revises: 0128
Create Date: 2026-05-29 22:45:00.000000

"""

from alembic import op


revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite fallback (also the form the index-parity invariant detects).
        op.create_index("ix_task_created_at", "task", ["created_at"])
        return
    # CONCURRENTLY cannot run inside a transaction; commit first.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_created_at
        ON task (created_at)
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index("ix_task_created_at", table_name="task")
        return
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_created_at")
