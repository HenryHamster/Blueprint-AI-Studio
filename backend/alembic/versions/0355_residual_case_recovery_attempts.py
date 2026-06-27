"""Add recovery_attempts to ResidualRemediationCase.

Revision ID: 0355
Revises: 0349
"""

from alembic import op
import sqlalchemy as sa


revision = "0355"
down_revision = "0349"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "residualremediationcase",
        sa.Column(
            "recovery_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("residualremediationcase", "recovery_attempts")
