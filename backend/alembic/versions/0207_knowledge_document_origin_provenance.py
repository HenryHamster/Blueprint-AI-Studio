"""knowledge_document_origin_provenance

Adds nullable, bounded origin-provenance columns to ``knowledgedocument`` so
design/web_research documents spawned from research findings can preserve the
source finding and second-opinion challenge record that led to dispatch.

All columns are nullable with no server default so legacy rows remain readable
and the migration is metadata-only for existing data. Indexes cover the bounded
list filters added by the shared knowledge APIs.

Revision ID: 0207
Revises: 0205
Create Date: 2026-06-09 05:59:46.197058

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0207"
down_revision = "0205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic autogenerate also emitted unrelated drops/type changes for known
    # metadata drift in older migrations. Those operations would be destructive,
    # so this revision keeps only the added KnowledgeDocument columns/indexes.
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_state", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_research_task_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column(
            "origin_parent_research_task_id", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_finding_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_finding_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_challenge_state", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "knowledgedocument",
        sa.Column("origin_challenge_record", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_document_origin_challenge_state",
        "knowledgedocument",
        ["origin_challenge_state"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_document_origin_finding_hash",
        "knowledgedocument",
        ["origin_finding_hash"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_document_origin_state",
        "knowledgedocument",
        ["origin_state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_document_origin_state", table_name="knowledgedocument")
    op.drop_index(
        "ix_knowledge_document_origin_finding_hash",
        table_name="knowledgedocument",
    )
    op.drop_index(
        "ix_knowledge_document_origin_challenge_state",
        table_name="knowledgedocument",
    )
    op.drop_column("knowledgedocument", "origin_challenge_record")
    op.drop_column("knowledgedocument", "origin_challenge_state")
    op.drop_column("knowledgedocument", "origin_finding_hash")
    op.drop_column("knowledgedocument", "origin_finding_index")
    op.drop_column("knowledgedocument", "origin_parent_research_task_id")
    op.drop_column("knowledgedocument", "origin_research_task_id")
    op.drop_column("knowledgedocument", "origin_state")
