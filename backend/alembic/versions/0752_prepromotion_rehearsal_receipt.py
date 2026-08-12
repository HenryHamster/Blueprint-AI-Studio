"""Persist candidate-bound release bootstrap rehearsal evidence.

Revision ID: 0752
Revises: 0751
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0752"
down_revision = "0751"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "prepromotion_rehearsal_receipt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "prepromotion_rehearsal_receipt")
