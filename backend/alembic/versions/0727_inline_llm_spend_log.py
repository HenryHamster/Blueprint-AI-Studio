"""Add append-only inline_llm_spend_log table.

Revision ID: 0727
Revises: 0725
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0727"
down_revision = "0725"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inline_llm_spend_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "cache_creation_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_read_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inline_llm_spend_log_provider_created_at",
        "inline_llm_spend_log",
        ["provider", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inline_llm_spend_log_provider_created_at",
        table_name="inline_llm_spend_log",
    )
    op.drop_table("inline_llm_spend_log")
