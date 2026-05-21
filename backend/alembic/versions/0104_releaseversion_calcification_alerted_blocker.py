"""Add calcification_alerted_blocker to releaseversion.

Stores the blocker code that was active when a calcification alert was last
posted for this version. Used by the calcification sweep to deduplicate alerts:
a new alert is only posted when the current blocker differs from this field
(or the field is NULL, meaning no alert has been posted yet). Storing dedup
state here avoids relying solely on Alert row existence, which can be lost
when an operator clears alerts.

Revision ID: 0104
Revises: 0103
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "calcification_alerted_blocker",
            sa.String(length=128),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "calcification_alerted_blocker")
