"""add_strategic_memory_notes

Add strategic_memory_notes JSONB column to the project table for durable
behavioral-memory storage. The resident strategist uses the set_memory op
to persist lessons and operator preferences across sessions.

Revision ID: 0448
Revises: 0447
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0448"
down_revision = "0447"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("strategic_memory_notes", JSONB, nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("project", "strategic_memory_notes")
