"""Add index on PoolVm.reserved_lane_id.

The warm-lane listing path queries PoolVm filtered by reserved_lane_id
(select(PoolVm).where(PoolVm.reserved_lane_id.in_(lane_ids))). Without an
index this degrades to a sequential scan of the entire PoolVm table on every
call, proportional to pool fleet size.

Revision ID: 0288
Revises: 0277
Create Date: 2026-06-15 00:00:00.000000
"""

from alembic import op

revision = "0288"
down_revision = "0277"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_poolvm_reserved_lane_id",
        "poolvm",
        ["reserved_lane_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_poolvm_reserved_lane_id", table_name="poolvm")
