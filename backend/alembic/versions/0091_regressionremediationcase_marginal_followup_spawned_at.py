"""Add marginal_followup_spawned_at to regressionremediationcase.

Durable cooldown anchor for the auto-spawned follow-up investigate task
that fires when a NEEDS_MANUAL_INTERVENTION case is closed as
CLOSE_REASON_RECOVERED_MARGINAL. The dispatcher reads this column to decide
whether the cooldown window (default 72 h) has expired before spawning
another follow-up — prevents infinite re-spawn loops if a follow-up itself
triggers a marginal closure.

Revision ID: 0091
Revises: 0090
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "marginal_followup_spawned_at",
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "marginal_followup_spawned_at")
