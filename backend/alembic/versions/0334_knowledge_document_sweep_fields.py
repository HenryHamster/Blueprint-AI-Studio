"""Add sweep_id and arm_label columns to knowledge_document for per-arm sweep indexing.

Revision ID: 0334
Revises: 0333
"""

import sqlalchemy as sa
from alembic import op


revision = "0334"
down_revision = "0333"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledgedocument",
        sa.Column("sweep_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("arm_label", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_knowledge_document_sweep_id",
        "knowledgedocument",
        ["sweep_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_document_sweep_id", table_name="knowledgedocument")
    op.drop_column("knowledgedocument", "arm_label")
    op.drop_column("knowledgedocument", "sweep_id")
