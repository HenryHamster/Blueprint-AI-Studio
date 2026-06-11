"""Add ImprovementLane control-plane table.

Revision ID: 0232
Revises: 0223
"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0232"
down_revision = "0223"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "improvementlane",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("direction_id", sa.String(length=64), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="PROPOSED"),
        sa.Column("branch_name", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("last_checkpoint_sha", sa.String(length=64), nullable=True),
        sa.Column("last_checkpoint_phase", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "checkpoints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("current_task_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("workspace_state", sa.String(length=32), nullable=False, server_default="COLD"),
        sa.Column("workspace_vm_instance_id", sa.String(length=255), nullable=True),
        sa.Column("phase_requeues", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_input_tokens_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_output_tokens_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("vm_cost_usd_total", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("reserved_idle_cost_usd_total", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("pilot_arm", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_improvementlane_project_status",
        "improvementlane",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_improvementlane_direction_status",
        "improvementlane",
        ["direction_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_improvementlane_direction_status", table_name="improvementlane")
    op.drop_index("ix_improvementlane_project_status", table_name="improvementlane")
    op.drop_table("improvementlane")
