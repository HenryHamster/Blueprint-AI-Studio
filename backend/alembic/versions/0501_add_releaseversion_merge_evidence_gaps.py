"""Add merge_evidence_gaps JSONB column to releaseversion.

Persists, per open release version, a cumulative tally of merged tasks that
failed to record criterion evidence, so operators can diagnose the gap without
trawling logs.

Revision ID: 0501
Revises: 0500
Create Date: 2026-07-11

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0501"
down_revision = "0500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "merge_evidence_gaps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "merge_evidence_gaps")
