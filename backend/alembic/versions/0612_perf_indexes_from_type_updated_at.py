"""Add performance indexes: ProjectMessage.from_type and ComputeJob.updated_at.

Revision ID: 0612
Revises: 0611
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op

revision = "0612"
down_revision = "0611"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Standalone index on from_type for cross-project queries (e.g. reaper sweeps
        # deleting system messages).  Added via index=True on the field in models.py.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_from_type "
            "ON projectmessage (from_type)"
        )
        # Composite status+from_type index for 15+ query sites that filter on both.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_status_from_type "
            "ON projectmessage (status, from_type)"
        )
        # ComputeJob.updated_at index for ORDER BY hot paths in strategic context
        # assembly (strategic_context_package.py) and strategic_runtime_work.py.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_computejob_updated_at "
            "ON computejob (updated_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_computejob_updated_at"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_projectmessage_status_from_type"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_projectmessage_from_type"
        )
