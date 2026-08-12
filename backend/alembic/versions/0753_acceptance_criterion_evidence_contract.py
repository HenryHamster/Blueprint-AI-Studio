"""Add explicit machine-evidence contract metadata to acceptance criteria.

Revision ID: 0753
Revises: 0752
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0753"
down_revision = "0752"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "versionacceptancecriterion",
        sa.Column(
            "evidence_contract",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("versionacceptancecriterion", "evidence_contract")
