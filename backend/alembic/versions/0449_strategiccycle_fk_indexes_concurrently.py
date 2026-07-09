"""strategiccycle_fk_indexes_concurrently

Re-create three StrategicCycle FK indexes with CREATE INDEX CONCURRENTLY
so that the indexes are built without an ACCESS EXCLUSIVE lock on a hot
table.  Migration 0447 created these indexes without CONCURRENTLY; on
installations that already ran 0447 the IF NOT EXISTS clause makes this
a no-op.  On fresh installations this revision runs before 0447 in the
chain, so the concurrent path applies safely.

- ix_strategiccycle_project_id   (strategiccycle.project_id)
- ix_strategiccycle_phase1_task_id (strategiccycle.phase1_task_id)
- ix_strategiccycle_phase2_task_id (strategiccycle.phase2_task_id)

SQLite test environments already have the indexes from 0447 — no-op here.

Revision ID: 0449
Revises: 0448
"""

from alembic import op


revision = "0449"
down_revision = "0448"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_strategiccycle_project_id ON strategiccycle (project_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_strategiccycle_phase1_task_id ON strategiccycle (phase1_task_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_strategiccycle_phase2_task_id ON strategiccycle (phase2_task_id)"
        )
    # SQLite: indexes already exist from 0447; nothing to do.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_strategiccycle_project_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_strategiccycle_phase1_task_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_strategiccycle_phase2_task_id")
    # SQLite: 0447's downgrade will drop the indexes.
