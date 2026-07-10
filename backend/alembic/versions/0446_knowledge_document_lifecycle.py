"""knowledge_document_lifecycle

Add lifecycle tracking columns to KnowledgeDocument so documents can be
marked as pending, consumed, or dismissed by the strategist follow-up
workflow. Existing rows default to 'pending'.

Revision ID: 0446
Revises: 0424
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa

revision = "0446"
down_revision = "0424"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.add_column(
        "knowledgedocument",
        sa.Column(
            "lifecycle_status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_document_lifecycle_status",
        "knowledgedocument",
        ["lifecycle_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_document_lifecycle_status")
    op.drop_column("knowledgedocument", "dismissed_reason")
    op.drop_column("knowledgedocument", "dismissed_at")
    op.drop_column("knowledgedocument", "consumed_at")
    op.drop_column("knowledgedocument", "lifecycle_status")
