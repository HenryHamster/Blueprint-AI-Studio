"""Add index on task.archived for hot-path WHERE clauses.

Revision ID: 0530
Revises: 0529
Create Date: 2026-07-12

Created CONCURRENTLY on Postgres so production rollouts don't block
writes on the ``task`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first to release Alembic's wrapping
transaction, then issue the DDL on the now-autocommit connection.

"""

from alembic import op

revision = "0530"
down_revision = "0529"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_archived ON task (archived)"
        )
    else:
        op.create_index("ix_task_archived", "task", ["archived"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_archived")
    else:
        op.drop_index("ix_task_archived", table_name="task")
