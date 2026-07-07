"""task_vm_stopped_at_index

Add partial index on Task.vm_stopped_at CONCURRENTLY to support range-filter
+ ORDER BY queries in anomaly_detection.py without a full sequential scan.

The base migration (0383) creates this index without CONCURRENTLY. This
revision re-issues it with IF NOT EXISTS so the CONCURRENTLY path applies on
new Postgres installations (avoiding an ACCESS EXCLUSIVE lock during deploy)
while being a no-op on installations already running 0383.

SQLite test environments already have the index from 0383 — no-op here.

Revision ID: 0384
Revises: 0383
"""

from alembic import op


revision = "0384"
down_revision = "0383"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_vm_stopped_at
            ON task (vm_stopped_at)
            WHERE vm_stopped_at IS NOT NULL
            """
        )
    # SQLite: index already exists from 0382; nothing to do.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_vm_stopped_at")
    # SQLite: 0382's downgrade will drop the index.
