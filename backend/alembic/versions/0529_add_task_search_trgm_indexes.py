"""Add trigram GIN indexes on task.title/description/latest_status_message for ILIKE search.

Without these indexes, every admin task-search query triggers a full sequential
table scan on the task table (growing O(n) with the table).  The indexes use
pg_trgm's gin_trgm_ops operator class to accelerate the ``ILIKE '%pattern%'``
pattern used by ``admin.py:get_project_tasks``.

Revision ID: 0529
Revises: 0528
Create Date: 2026-07-12

"""

from alembic import op

revision = "0529"
down_revision = "0528"
branch_labels = None
depends_on = None

# Index definitions (DRY across upgrade/downgrade)
_TITLE_IDX = "ix_task_title_trgm"
_DESC_IDX = "ix_task_description_trgm"
_STATUS_MSG_IDX = "ix_task_latest_status_message_trgm"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("COMMIT")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_TITLE_IDX} "
            "ON task USING gin (title gin_trgm_ops)"
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_DESC_IDX} "
            "ON task USING gin (description gin_trgm_ops)"
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_STATUS_MSG_IDX} "
            "ON task USING gin (latest_status_message gin_trgm_ops)"
        )
    else:
        # SQLite test environments — plain indexes without gin_trgm_ops.
        # The op.create_index() call both creates the index AND registers it
        # for test_model_indexes_covered_by_migrations.
        op.create_index(_TITLE_IDX, "task", ["title"], postgresql_using="gin",
                        postgresql_ops={"title": "gin_trgm_ops"})
        op.create_index(_DESC_IDX, "task", ["description"], postgresql_using="gin",
                        postgresql_ops={"description": "gin_trgm_ops"})
        op.create_index(_STATUS_MSG_IDX, "task", ["latest_status_message"],
                        postgresql_using="gin",
                        postgresql_ops={"latest_status_message": "gin_trgm_ops"})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_TITLE_IDX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_DESC_IDX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_STATUS_MSG_IDX}")
    else:
        op.drop_index(_TITLE_IDX, table_name="task")
        op.drop_index(_DESC_IDX, table_name="task")
        op.drop_index(_STATUS_MSG_IDX, table_name="task")
