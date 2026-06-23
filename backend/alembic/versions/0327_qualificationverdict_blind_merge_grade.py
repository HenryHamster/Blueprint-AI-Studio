"""Add QualificationVerdict.blind_merge_grade JSONB column.

Durably preserves the blind merge-grade certificate computed during release
finalization in the immutable per-verdict calibration trail, mirroring the
existing theme_delivery column.  Purely additive: the column is nullable, so
existing verdict rows carry no blind merge-grade data and older qualify paths
(where the grade was not computed) persist NULL.

Revision ID: 0327
Revises: 0310
Create Date: 2026-06-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# down_revision anchors to 0317 (task_llm_account_id_column), the current
# linear chain head after the automain merge, keeping the chain single-headed.
revision = "0327"
down_revision = "0317"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "blind_merge_grade",
            JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "blind_merge_grade")
