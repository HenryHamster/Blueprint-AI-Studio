"""Add runner_heartbeat_snapshot time-series table.

LocalRunner.capabilities holds only the latest heartbeat telemetry — each
heartbeat overwrites the previous values, so there is no history to trend GPU/
disk/memory pressure over time, and per-runner outcome rollups can only be
computed by scanning every ComputeJob row. This table captures one append-only
row per heartbeat (fire-and-forget on the heartbeat path) so the Fleet panel
can sparkline recent trends. Rows are retention-trimmed (default 7 days) by a
periodic sweep; they are pure telemetry and never a dispatch source of truth.

Additive: brand-new table, no existing data to migrate. FK to localrunner with
ON DELETE CASCADE so snapshots never outlive their runner.

Revision ID: 0500
Revises: 0492
Create Date: 2026-07-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0500"
down_revision = "0493"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner_heartbeat_snapshot",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("runner_id", sa.UUID(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("gpu_utilization_pct", sa.Integer(), nullable=True),
        sa.Column("disk_free_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_free_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cpu_load_avg", sa.Float(), nullable=True),
        sa.Column(
            "active_task_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "active_compute_job_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("agent_available", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["runner_id"], ["localrunner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Retention-cleanup sweep deletes globally by age: WHERE captured_at < cutoff.
    op.create_index(
        "ix_runner_heartbeat_snapshot_captured_at",
        "runner_heartbeat_snapshot",
        ["captured_at"],
    )
    # Per-runner recent-window query (one runner, last hour) + FK prefix.
    op.create_index(
        "ix_runner_heartbeat_snapshot_runner_captured",
        "runner_heartbeat_snapshot",
        ["runner_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runner_heartbeat_snapshot_runner_captured",
        table_name="runner_heartbeat_snapshot",
    )
    op.drop_index(
        "ix_runner_heartbeat_snapshot_captured_at",
        table_name="runner_heartbeat_snapshot",
    )
    op.drop_table("runner_heartbeat_snapshot")
