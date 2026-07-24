"""Add project_efficiency_snapshot, project_lane_baseline, project_sr_analytics tables.

Revision ID: 0658
Revises: 0657
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0658"
down_revision = "0657"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_efficiency_snapshot",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
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
    op.create_table(
        "project_lane_baseline",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
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
    op.create_table(
        "project_sr_analytics",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
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


def downgrade():
    op.drop_table("project_sr_analytics")
    op.drop_table("project_lane_baseline")
    op.drop_table("project_efficiency_snapshot")
