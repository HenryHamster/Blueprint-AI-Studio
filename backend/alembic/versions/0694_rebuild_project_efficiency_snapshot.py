"""Rebuild project_efficiency_snapshot with per-captured_at multi-row schema.

Migration 0658 created project_efficiency_snapshot as a simple one-row-per-project
table (project_id PK, data JSONB). The accessor layer (project_telemetry.py)
and efficiency_snapshot.py require a multi-row time-series schema with
captured_at, metric_def_version, and blob columns. This migration drops the
existing table and recreates it with the correct structure.

Revision ID: 0694
Revises: 0692
Create Date: 2026-07-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0694"
down_revision = "0692"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("project_efficiency_snapshot")
    op.create_table(
        "project_efficiency_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric_def_version", sa.String(length=64), nullable=False),
        sa.Column(
            "blob",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "captured_at", name="uq_proj_efficiency_snap"),
    )
    op.create_index(
        "ix_project_efficiency_snapshot_project_id",
        "project_efficiency_snapshot",
        ["project_id"],
    )


def downgrade():
    op.drop_index(
        "ix_project_efficiency_snapshot_project_id",
        table_name="project_efficiency_snapshot",
    )
    op.drop_table("project_efficiency_snapshot")
    op.create_table(
        "project_efficiency_snapshot",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )
