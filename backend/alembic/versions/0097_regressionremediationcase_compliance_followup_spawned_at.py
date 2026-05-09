"""Add compliance_followup_spawned_at to regressionremediationcase.

Durable cooldown anchor for the auto-spawned compliance follow-up investigate
task that fires when a cross-layer-mandated fix merges but only targeted fewer
than 2 distinct canonical layers. The dispatcher reads this column to decide
whether the cooldown window has expired before spawning another compliance
follow-up — prevents repeat spawns for the same non-compliant fix.

Revision ID: 0097
Revises: 0096
Create Date: 2026-05-09 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "compliance_followup_spawned_at",
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "compliance_followup_spawned_at")
