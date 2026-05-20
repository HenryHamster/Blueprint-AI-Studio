"""Add Task efficiency telemetry columns.

Adds three new columns to the Task table for VM provisioning and LLM
efficiency tracking:
- vm_cold_start_ms: milliseconds from VM provisioning to first agent
  heartbeat. Recorded once per VM lifecycle and guarded by IS NULL so
  subsequent vm_provisioned_at stamping does not overwrite it.
- vm_warm_start: boolean flag indicating if VM was reused from a warm
  pool or freshly provisioned. Defaults to False; scaffolding for
  future warm-pooling feature.
- api_call_summary: JSONB with cumulative LLM telemetry posted on
  every heartbeat. Null for older agents. Shape:
  {"llm_call_count": int, "total_input_tokens": int,
   "total_output_tokens": int, "last_updated_at": iso8601}

All columns nullable; existing rows leave them NULL until the next
heartbeat populates them.

Revision ID: 0095
Revises: 0094
Create Date: 2026-05-09 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column(
            "vm_cold_start_ms",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "task",
        sa.Column(
            "vm_warm_start",
            sa.Boolean(),
            nullable=True,
            server_default="false",
        ),
    )
    op.add_column(
        "task",
        sa.Column(
            "api_call_summary",
            JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("task", "api_call_summary")
    op.drop_column("task", "vm_warm_start")
    op.drop_column("task", "vm_cold_start_ms")
