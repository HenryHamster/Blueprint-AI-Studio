"""Add expression index on task payloads[0]->>'type' covering all statuses.

Revision ID: 0238
Revises: 0237
"""

from alembic import op


revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite fallback — column-only index keeps metadata invariants happy
        # in tests; expression-on-JSON path filtering is Postgres-only.
        op.create_index("ix_task_payloads_type_all", "task", ["payloads"])
        return
    # CONCURRENTLY cannot run inside a transaction block. Commit the
    # alembic migration's open transaction first.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_payloads_type_all
        ON task ((payloads->0->>'type'))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index("ix_task_payloads_type_all", table_name="task")
        return
    op.execute("COMMIT")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_payloads_type_all")
