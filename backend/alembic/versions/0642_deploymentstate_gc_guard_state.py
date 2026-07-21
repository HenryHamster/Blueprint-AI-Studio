"""Add gc_guard_state JSONB column to deploymentstate.

Persists the cumulative GC-guard sweep counters (total_runs,
consecutive_healthy_cycles, etc.) so the operator readiness signal
(get_compute_image_gc_guard_readiness) survives process restarts/redeploys
instead of resetting to zero every time.

Column is nullable; NULL means no sweep has completed since the column
was added (existing rows default to NULL on upgrade).

Revision ID: 0642
Revises: 0641
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0642"
down_revision = "0641"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deploymentstate",
        sa.Column("gc_guard_state", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deploymentstate", "gc_guard_state")
