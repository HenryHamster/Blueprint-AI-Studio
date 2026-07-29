"""Fence durable conversation claims by Resident process incarnation.

Revision ID: 0710
Revises: 0709
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0710"
down_revision = "0709"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectmessage",
        sa.Column(
            "conversation_claim_incarnation",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("projectmessage", "conversation_claim_incarnation")
