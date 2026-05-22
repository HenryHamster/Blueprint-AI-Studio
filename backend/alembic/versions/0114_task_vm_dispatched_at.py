"""Add vm_dispatched_at to task for full dispatch->agent-ready cold-start measurement.

The existing vm_cold_start_ms column was anchored at vm_provisioned_at, which is
re-stamped at provision-confirm time (after GCE API reports VM running). This
excluded the GCE boot latency — the dominant cold-start component. A separate
write-once vm_dispatched_at column captures the orchestrator's initial dispatch
timestamp so vm_cold_start_ms can cover the full dispatch->agent-ready interval.

Revision ID: 0113
Revises: 0112
Create Date: 2026-05-22 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("vm_dispatched_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "vm_dispatched_at")
