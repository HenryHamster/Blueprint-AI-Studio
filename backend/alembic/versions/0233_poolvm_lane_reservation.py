"""Add lane-scoped workspace reservation columns to PoolVm.

Revision ID: 0233
Revises: 0232
"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


revision = "0233"
down_revision = "0232"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain GUID nullable columns — no FK, mirrors leased_task_id pattern.
    op.add_column(
        "poolvm",
        sa.Column(
            "reserved_lane_id",
            sqlmodel.sql.sqltypes.GUID(),
            nullable=True,
        ),
    )
    op.add_column(
        "poolvm",
        sa.Column(
            "reservation_expires_at",
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("poolvm", "reservation_expires_at")
    op.drop_column("poolvm", "reserved_lane_id")
