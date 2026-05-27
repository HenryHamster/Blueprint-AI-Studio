"""Add QualificationVerdict.audit_missing_reason column.

Surfaces the reason an audit sub-score was missing or unscoreable
(e.g. parseable_but_unscoreable) directly on the verdict row so admin
endpoints can expose it without reading JSONB blobs.

Revision ID: 0126
Revises: 0125
Create Date: 2026-05-27 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qualificationverdict",
        sa.Column("audit_missing_reason", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qualificationverdict", "audit_missing_reason")
