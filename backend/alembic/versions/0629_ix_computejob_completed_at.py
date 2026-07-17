"""Add index on computejob.completed_at for viewable-artifact retirement sweep.

The reaper sweep_retire_viewable_artifacts queries ComputeJobFile rows whose
owning ComputeJob.completed_at is older than the retention window (default
180 days). Without an index on completed_at the inner subquery requires a
full table scan of computejob on every sweep iteration.

Revision ID: 0629
Revises: 0628
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "0629"
down_revision = "0628"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_computejob_completed_at "
            "ON computejob (completed_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_computejob_completed_at"
        )
