"""Add QualificationVerdict.theme_delivery JSONB column.

Durably preserves the theme-delivery certification computed during release
finalization in the immutable per-verdict calibration trail.  Purely additive:
the column is nullable, so existing verdict rows carry no theme_delivery data
and older qualify paths (where the cert was not computed) persist NULL.

Revision ID: 0305
Revises: 0300
Create Date: 2026-06-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0305"
down_revision = "0300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "theme_delivery",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "theme_delivery")
