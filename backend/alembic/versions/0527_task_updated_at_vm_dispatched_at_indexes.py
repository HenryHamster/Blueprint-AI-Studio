"""task_updated_at_vm_dispatched_at_indexes

Add partial index on Task.vm_dispatched_at (non-NULL only) to support
hot-path queries that filter/order by this column.  The companion
ix_task_updated_at index was created by migration 0450; this migration
only adds the vm_dispatched_at partial index.

- ix_task_vm_dispatched_at   (task.vm_dispatched_at, WHERE vm_dispatched_at IS NOT NULL)

Chained onto the CURRENT head rather than spliced in between 0458 and 0459.
An earlier attempt inserted this as "0458b" mid-chain, which required editing
the already-deployed 0459's down_revision in place — Alembic silently skips a
body-edited applied revision, so this index would never have run against a live
database, and the qualifier's migration-identity gate (correctly) hard-blocked
promotion. The index is order-independent, so appending it at the head is
equivalent and leaves applied history immutable.

Revision ID: 0527
Revises: 0526
"""
from alembic import op
import sqlalchemy as sa

revision = "0527"
down_revision = "0526"
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
