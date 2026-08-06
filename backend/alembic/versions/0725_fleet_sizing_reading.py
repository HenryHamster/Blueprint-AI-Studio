"""Add append-only fleet_sizing_reading table.

Revision ID: 0725
Revises: 0724
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0725"
down_revision = "0724"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fleet_sizing_reading",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("serving_sha", sa.String(length=64), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "blob",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fleet_sizing_reading_captured_at",
        "fleet_sizing_reading",
        ["captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fleet_sizing_reading_captured_at", table_name="fleet_sizing_reading")
    op.drop_table("fleet_sizing_reading")
