"""Add QualificationVerdict.goal_alignment JSONB column.

Durably preserves the goal-alignment grade computed during release
finalization in the immutable per-verdict calibration trail.  Purely additive:
the column is nullable, so existing verdict rows carry no goal_alignment data
and older qualify paths persist NULL.

Revision ID: 0328
Revises: 0327
Create Date: 2026-06-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0328"
down_revision = "0327"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "goal_alignment",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "goal_alignment")
