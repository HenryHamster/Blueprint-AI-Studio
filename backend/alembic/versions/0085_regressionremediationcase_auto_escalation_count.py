"""Add auto_escalation_count to regressionremediationcase.

Tracks the number of autonomous escalations fired by the dispatcher watchdog
before promoting a stalled remediation case to NEEDS_MANUAL_INTERVENTION.

Revision ID: 0085
Revises: 0084
Create Date: 2026-05-07 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "auto_escalation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "auto_escalation_count")
