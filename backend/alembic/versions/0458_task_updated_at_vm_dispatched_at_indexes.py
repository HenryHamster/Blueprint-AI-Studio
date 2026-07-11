"""task_updated_at_vm_dispatched_at_indexes

Add partial index on Task.vm_dispatched_at (non-NULL only) to support
hot-path queries that filter/order by this column.  The companion
ix_task_updated_at index was created by migration 0450; this migration
only adds the vm_dispatched_at partial index.

- ix_task_vm_dispatched_at   (task.vm_dispatched_at, WHERE vm_dispatched_at IS NOT NULL)

Revision ID: 0458
Revises: 0457
"""
from alembic import op
import sqlalchemy as sa

revision = "0458"
down_revision = "0457"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_task_vm_dispatched_at ON task (vm_dispatched_at) "
            "WHERE vm_dispatched_at IS NOT NULL"
        )
    else:
        op.create_index(
            "ix_task_vm_dispatched_at",
            "task",
            ["vm_dispatched_at"],
            postgresql_where=sa.text("vm_dispatched_at IS NOT NULL"),
            sqlite_where=sa.text("vm_dispatched_at IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_vm_dispatched_at"
        )
    else:
        op.drop_index("ix_task_vm_dispatched_at", table_name="task")
