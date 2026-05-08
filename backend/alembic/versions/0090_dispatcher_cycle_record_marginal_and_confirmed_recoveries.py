"""Add marginal and confirmed recovery counts to DispatcherCycleRecord.

Tracks the marginal vs confirmed recovery rate across cycles to surface
trending and emit warnings when the marginal-recovery rate exceeds
configurable thresholds for consecutive cycles.

Revision ID: 0090
Revises: 0089
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dispatchercyclerecord",
        sa.Column(
            "marginal_recoveries_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "dispatchercyclerecord",
        sa.Column(
            "confirmed_recoveries_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dispatchercyclerecord", "confirmed_recoveries_count")
    op.drop_column("dispatchercyclerecord", "marginal_recoveries_count")
