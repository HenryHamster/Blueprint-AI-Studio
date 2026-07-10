"""add ix_task_updated_at index

Task.updated_at is queried in ~15 locations including reaper staleness
sweeps (ORDER BY updated_at ASC), strategic spawning (ORDER BY updated_at
DESC), operator attention queries, and work session recheck queries.
Without a standalone index every staleness sweep degrades to a full sort
of the entire tasks table.

Created CONCURRENTLY on Postgres to avoid blocking concurrent writes.

Revision ID: 0450
Revises: 0449
"""

from alembic import op

revision = "0450"
down_revision = "0449"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_task_updated_at ON task (updated_at)"
        )
    # SQLite test environments: no CONCURRENTLY needed; simple CREATE.
    else:
        op.create_index("ix_task_updated_at", "task", ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
    op.drop_index("ix_task_updated_at", table_name="task", if_exists=True)
