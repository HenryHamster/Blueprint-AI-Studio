"""Persist truthful provenance for deterministic category verdicts.

Revision ID: 0754
Revises: 0753
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0754"
down_revision = "0753"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "categoryverdict",
        sa.Column(
            "evidence_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("categoryverdict", "evidence_provenance")
