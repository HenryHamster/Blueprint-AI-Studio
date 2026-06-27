"""Drop the release-version delivery-theme enum layer.

Removes the bounded delivery-theme abstraction:
  * releaseversion.theme + releaseversion.theme_editor_info — the
    strategist-authored delivery-shape enum and its editor provenance.
  * qualificationverdict.theme_delivery — the theme-delivery certification
    JSONB stamped at qualify-time.

All three columns were nullable and advisory (the theme-delivery gate shipped
dark with theme_block_threshold=0.0), so dropping them removes no enforced
behavior. The downgrade re-adds the columns nullable; historical values are
not restored.

Revision ID: 0356
Revises: 0355
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0356"
down_revision = "0355"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("qualificationverdict", "theme_delivery")
    op.drop_column("releaseversion", "theme_editor_info")
    op.drop_column("releaseversion", "theme")


def downgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column("theme", sa.String(32), nullable=True),
    )
    op.add_column(
        "releaseversion",
        sa.Column("theme_editor_info", sa.String(255), nullable=True),
    )
    op.add_column(
        "qualificationverdict",
        sa.Column(
            "theme_delivery",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
