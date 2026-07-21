"""Add AuditLaneDecisionLog table for durable observe-mode evidence.

Durable per-cycle log for structural_health_audit and metrics_integrity_sweep.
Stores one row per audit cycle / sweep tick (both lanes, all modes) so the
observe-window 'would-have-fired' count survives process restarts and deploys.

Revision ID: 0643
Revises: 0642
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0643"
down_revision = "0642"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auditlanedecisionlog",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lane", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("enrolled", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auditlanedecisionlog_ts", "auditlanedecisionlog", ["ts"])
    op.create_index(
        "ix_auditlanedecisionlog_lane_ts",
        "auditlanedecisionlog",
        ["lane", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_auditlanedecisionlog_lane_ts", table_name="auditlanedecisionlog")
    op.drop_index("ix_auditlanedecisionlog_ts", table_name="auditlanedecisionlog")
    op.drop_table("auditlanedecisionlog")
