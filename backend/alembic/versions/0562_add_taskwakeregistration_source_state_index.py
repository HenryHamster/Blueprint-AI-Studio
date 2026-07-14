"""Add composite index on taskwakeregistration(wake_source_kind, wake_source_ref, delivery_state).

Revision ID: 0562
Revises: 0561
Create Date: 2026-07-14

fire_task_wakes() (backend/app/task_lifecycle.py) point-looks-up PENDING
registrations by (wake_source_kind, wake_source_ref) on every compute-job
terminal transition. Only task_id and project_id are indexed today, so this
hot firing filter seq-scans the ``taskwakeregistration`` table, which grows
with every parked task. Add a composite (wake_source_kind, wake_source_ref,
delivery_state) index covering the firing WHERE clause.

Created CONCURRENTLY on Postgres so production rollouts don't block writes on
the ``taskwakeregistration`` table. CONCURRENTLY cannot run inside a
transaction, so we issue ``COMMIT`` first to release Alembic's wrapping
transaction, then issue the DDL on the now-autocommit connection.

"""

from alembic import op

revision = "0562"
down_revision = "0561"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_taskwakeregistration_source_state "
            "ON taskwakeregistration "
            "(wake_source_kind, wake_source_ref, delivery_state)"
        )
    else:
        op.create_index(
            "ix_taskwakeregistration_source_state",
            "taskwakeregistration",
            ["wake_source_kind", "wake_source_ref", "delivery_state"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_taskwakeregistration_source_state"
        )
    else:
        op.drop_index(
            "ix_taskwakeregistration_source_state",
            table_name="taskwakeregistration",
        )
