"""ComputeJob last_heartbeat_at column.

Adds ``last_heartbeat_at`` to the ``computejob`` table so GCP compute-job
VMs can push liveness signals via POST /api/agent/compute-jobs/{id}/heartbeat
instead of relying on serial-console polling or the GCP insert LRO.

The orchestrator monitors this field to guard the provisioning timeout: a VM
that has heartbeated is never declared failed or orphan-reaped, even when the
GCP insert long-running-operation is still in flight.  The first heartbeat
also transitions the job from STARTING to RUNNING without waiting for the LRO.

Revision ID: 0309
Revises: 0308
"""

from alembic import op
import sqlalchemy as sa


revision = "0309"
down_revision = "0308"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "computejob",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("computejob", "last_heartbeat_at")
