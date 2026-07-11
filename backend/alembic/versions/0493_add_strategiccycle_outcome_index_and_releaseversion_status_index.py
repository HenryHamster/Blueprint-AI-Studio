"""Add cross-project indexes for StrategicCycle and ReleaseVersion.

Two performance indexes:

1. ix_strategiccycle_outcome_phase1_started on (outcome, phase1_started_at)
   — covers the stalled-cycle reaper scan that has no project_id filter.

2. ix_releaseversion_status on (status) — covers cross-project ReleaseVersion
   scans in metrics sweep, release qualifier sweep, and release_version.py.

Revision ID: 0493
Revises: 0492
Create Date: 2026-07-11

"""

from alembic import op

revision = "0493"
down_revision = "0492"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_strategiccycle_outcome_phase1_started",
        "strategiccycle",
        ["outcome", "phase1_started_at"],
    )
    op.create_index(
        "ix_releaseversion_status",
        "releaseversion",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategiccycle_outcome_phase1_started", table_name="strategiccycle")
    op.drop_index("ix_releaseversion_status", table_name="releaseversion")
