"""Add operator_acknowledged_at to regressionremediationcase.

Records when an operator first acknowledged a NEEDS_MANUAL_INTERVENTION
escalation. NULL for historical cases and unacknowledged cases.
Set once via the POST /regression-cases/{id}/acknowledge endpoint;
never overwritten once stamped. Enables MTTI metric:
needs_manual_intervention_at → operator_acknowledged_at.

Revision ID: 0093
Revises: 0092
Create Date: 2026-05-08 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "operator_acknowledged_at",
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "operator_acknowledged_at")
