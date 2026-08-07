"""Add pricing_status to inline_llm_spend_log; make cost_usd nullable.

Revision ID: 0728
Revises: 0727
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0728"
down_revision = "0727"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inline_llm_spend_log",
        sa.Column(
            "pricing_status",
            sa.String(length=16),
            nullable=False,
            server_default="priced",
        ),
    )
    # Unpriced-model calls are recorded with their usage and cost_usd NULL
    # (unknown) instead of a fabricated zero, so consumers cannot mistake a
    # dark-spend row for a real measurement.
    op.alter_column(
        "inline_llm_spend_log",
        "cost_usd",
        existing_type=sa.Float(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "inline_llm_spend_log",
        "cost_usd",
        existing_type=sa.Float(),
        nullable=False,
    )
    op.drop_column("inline_llm_spend_log", "pricing_status")
