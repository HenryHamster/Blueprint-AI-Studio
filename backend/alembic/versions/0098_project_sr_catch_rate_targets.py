"""Add sr_catch_rate_targets to Project.

Operators can declare per-category minimum SR hit-rates, e.g.
{'security': 0.80, 'regression_risk': 0.75}. Absent key = no target
configured. Consumed by the digest alert layer to fire below-floor alerts.

Revision ID: 0098
Revises: 0097
Create Date: 2026-05-09 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "sr_catch_rate_targets",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("project", "sr_catch_rate_targets")
