"""Add symptoms_by_layer JSONB column to regressionremediationcase.

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-07 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regressionremediationcase",
        sa.Column(
            "symptoms_by_layer",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("regressionremediationcase", "symptoms_by_layer")
