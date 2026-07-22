"""Rebuild project_lane_baseline with per-version multi-row schema.

Migration 0658 created project_lane_baseline as a simple one-row-per-project
table (project_id PK, data JSONB). The accessor layer (project_telemetry.py)
and lane_baseline_snapshot.py require a multi-row schema with version_number,
captured_at, baseline_def_version, and blob columns. This migration drops the
empty table and recreates it with the correct structure.

Revision ID: 0681
Revises: 0680
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0681"
down_revision = "0680"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("project_lane_baseline")
    op.create_table(
        "project_lane_baseline",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_def_version", sa.String(length=64), nullable=False),
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
        sa.UniqueConstraint("project_id", "version_number", name="uq_proj_lane_baseline"),
    )
    op.create_index(
        "ix_project_lane_baseline_project_id",
        "project_lane_baseline",
        ["project_id"],
    )


def downgrade():
    op.drop_index("ix_project_lane_baseline_project_id", table_name="project_lane_baseline")
    op.drop_table("project_lane_baseline")
    op.create_table(
        "project_lane_baseline",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
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
