"""Add regression_signal_state JSONB column to project for MTTD first-tripped timestamps.

Revision ID: 0084
Revises: 0083
Create Date: 2026-05-06 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "regression_signal_state",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("project", "regression_signal_state")
