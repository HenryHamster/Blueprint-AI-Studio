"""Add partial composite index on task(project_id, created_at) for the
investigate-task funnel.

The investigate-task funnel query in admin_digest filters by project_id and
``payloads[0]['type'] == 'investigate'`` over a created_at window. Without an
index the planner does a full-table scan for every digest request.

The index is *partial* — restricted to ``(payloads -> 0 ->> 'type') =
'investigate'`` — rather than indexing the type expression as a key column.
That keeps the index compact (only investigate rows) and, critically, keeps
the ``payloads -> 0 ->> 'type'`` expression out of ANALYZE's per-column
statistics. A non-partial expression index on that expression makes ANALYZE
gather full-table statistics for it, which then shadow the partial-index
statistics the orchestrator's pipeline-budget plan relies on
(``ix_task_payloads_type_active``) and flip that plan off the index.

Revision ID: 0113
Revises: 0112
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op


revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_task_project_payloads_type_created_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite fallback — a plain column index keeps migration-invariant
        # tests happy; partial JSON-expression predicates are Postgres-only.
        op.create_index(_INDEX_NAME, "task", ["project_id", "created_at"])
        return
    # CONCURRENTLY cannot run inside a transaction; commit first.
    op.execute("COMMIT")
    op.execute(
        f"""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME}
        ON task (project_id, created_at)
        WHERE (payloads -> 0 ->> 'type') = 'investigate'
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index(_INDEX_NAME, table_name="task")
        return
    op.execute("COMMIT")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
