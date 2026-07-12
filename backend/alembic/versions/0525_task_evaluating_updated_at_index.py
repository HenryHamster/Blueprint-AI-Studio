"""Add partial (updated_at) index for the EVALUATING CI-monitor scan.

The CI monitor sweep (orchestrator_ci_monitor.py:289-303) repeatedly selects
``Task`` rows in ``EVALUATING`` on the leader's hot cycle. The existing
``ix_task_updated_at`` is a full index over every task; a partial index over
only the (usually small) ``EVALUATING`` subset lets the planner walk just those
rows — the same partial-by-status shape already used by ``ix_task_blocked_recheck``
(WHERE status = 'BLOCKED') and ``ix_task_terminal_last_heartbeat``.

Created CONCURRENTLY on Postgres so the build does not block concurrent writes
on the hot ``task`` table. CONCURRENTLY cannot run inside a transaction, so we
issue ``op.execute("COMMIT")`` first to escape Alembic's per-migration
transaction (env.py sets ``transaction_per_migration=True``). The SQLite test
branch uses a plain ``op.create_index`` so the metadata Index is registered by
``test_model_indexes_covered_by_migrations``.

Revision ID: 0525
Revises: 0503
Create Date: 2026-07-12
"""

from alembic import op

revision = "0525"
down_revision = "0503"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction — commit Alembic's
        # per-migration transaction first, then build on the autocommit
        # connection.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_evaluating_updated_at "
            "ON task (updated_at) WHERE status = 'EVALUATING'"
        )
    else:
        # SQLite test environments: no CONCURRENTLY needed; plain create so the
        # coverage scanner registers the index name + columns.
        op.create_index("ix_task_evaluating_updated_at", "task", ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_evaluating_updated_at"
        )
    else:
        op.drop_index("ix_task_evaluating_updated_at", table_name="task")
