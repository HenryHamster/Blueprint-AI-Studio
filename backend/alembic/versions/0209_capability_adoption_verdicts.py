"""capability_adoption_verdicts

Adds nullable ``capability_adoption_verdicts`` JSONB column to ``project`` so the
pipeline can durably store adopt-or-reject verdicts for each capability trial.

Shape: {normalized_cap_name: {verdict, lift, n_with, n_without, window_hours,
evaluated_at, thresholds}} — newest verdict supersedes the prior one.
NULL = no trial verdict recorded yet for any capability.

Revision ID: 0209
Revises: 0208
Create Date: 2026-06-09

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("capability_adoption_verdicts", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project", "capability_adoption_verdicts")
