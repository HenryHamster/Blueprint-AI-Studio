"""Add investigate_spawned_at column to regressionremediationcase.

Tracks when the dispatcher first spawned an investigate Task for this
regression case. Null for cases pre-dating this column. Never overwritten
on re-spawns so operators always see the original detection-to-action
latency.

Revision ID: 0088
Revises: 0087
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "investigate_spawned_at",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "investigate_spawned_at")
