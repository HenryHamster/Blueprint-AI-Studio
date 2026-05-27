"""Add partial index ix_task_queued_no_approval for dispatch hot path.

Serves the orchestrator dispatch query pattern:
  WHERE status = 'QUEUED' AND requires_approval = false
  ORDER BY priority, created_at

Without this index the planner must scan all QUEUED rows and post-filter on
requires_approval. The partial index restricts the index pages to
requires_approval=false rows only, so the dispatch loop hits a tight index
range scan ordered by (status, priority, created_at).

Revision ID: 0116
Revises: 0115
Create Date: 2026-05-23 00:00:00.000000

"""

from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; commit first.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_queued_no_approval "
            "ON task (status, priority, created_at) WHERE requires_approval = false"
        )
    else:
        # SQLite (test path): plain B-tree composite so
        # test_model_indexes_covered_by_migrations can detect it via the
        # literal index name in op.create_index.
        op.create_index(
            "ix_task_queued_no_approval",
            "task",
            ["status", "priority", "created_at"],
            postgresql_where="requires_approval = false",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_queued_no_approval")
    else:
        op.drop_index("ix_task_queued_no_approval", table_name="task")
