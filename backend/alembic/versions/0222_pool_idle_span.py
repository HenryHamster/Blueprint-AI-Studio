"""Add PoolIdleSpan table for per-span windowed pool idle cost attribution.

Each settled idle span gets a durable row so _compute_windowed_idle_cost can
filter on settled_at rather than PoolIdleCostLedger.retired_at, eliminating
lump-attribution distortion for long-lived pool members.

Revision ID: 0222
Revises: 0207
"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


revision = "0222"
down_revision = "0209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "poolidlespan",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("vm_instance_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_poolidlespan_project_id",
        "poolidlespan",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_poolidlespan_settled_at",
        "poolidlespan",
        ["settled_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_poolidlespan_settled_at", table_name="poolidlespan")
    op.drop_index("ix_poolidlespan_project_id", table_name="poolidlespan")
    op.drop_table("poolidlespan")
