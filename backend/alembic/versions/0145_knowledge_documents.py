"""Add the durable knowledge-document store (``knowledgedocument``).

Blueprint declares ``output_kind='knowledge'`` on several read-only task types,
but historically that output had no durable, queryable home — it lived only as
ephemeral ``Task.plan_output`` read back through a narrow recent-findings
window. The new ``web_research`` task type instead persists a first-class
``KnowledgeDocument`` row: a titled, summarized markdown document with web/file
citations, gated by the forward-quality evaluator before it is stored.

This migration is the Postgres path for the ``KnowledgeDocument`` SQLModel
table (the local SQLite test fixture builds it from
``SQLModel.metadata.create_all``). Per CLAUDE.md it chains from the latest head
(0144) and ``EXPECTED_SCHEMA_REVISION`` is bumped to 0145 to match.

Search is index-backed exactly like ``Artifact.pr_url`` (migration 0122): the
``pg_trgm`` extension plus GIN trigram indexes on ``title`` and ``body`` make
project-scoped ILIKE / similarity search fast; a plain b-tree index on ``topic``
supports topic filtering. The two FK columns carry their own b-tree indexes
(mirroring the ``index=True`` model fields).

Revision ID: 0145
Revises: 0144
Create Date: 2026-06-04 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Idempotent guard: skip the create when the table already exists (mirrors
    # the 0140 back-merge pattern) so a re-applied / back-merged revision can
    # still advance ``alembic_version`` instead of raising DuplicateTableError.
    if sa.inspect(bind).has_table("knowledgedocument"):
        return

    # SQLModel maps ``KnowledgeDocument`` to ``knowledgedocument`` by default.
    op.create_table(
        "knowledgedocument",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id"),
            nullable=False,
        ),
        sa.Column(
            "source_task_id",
            sa.Uuid(),
            sa.ForeignKey("task.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column(
            "tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "source_citations",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("forward_quality_verdict", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # FK b-tree indexes (mirror the index=True model fields).
    op.create_index(
        "ix_knowledgedocument_project_id", "knowledgedocument", ["project_id"]
    )
    op.create_index(
        "ix_knowledgedocument_source_task_id", "knowledgedocument", ["source_task_id"]
    )
    # Plain topic index for exact/prefix topic filtering.
    op.create_index(
        "ix_knowledge_document_topic", "knowledgedocument", ["topic"]
    )

    # Trigram GIN indexes for ILIKE / similarity search over title + body.
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.create_index(
            "ix_knowledge_document_title_trgm",
            "knowledgedocument",
            ["title"],
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        )
        op.create_index(
            "ix_knowledge_document_body_trgm",
            "knowledgedocument",
            ["body"],
            postgresql_using="gin",
            postgresql_ops={"body": "gin_trgm_ops"},
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.drop_index(
            "ix_knowledge_document_body_trgm", table_name="knowledgedocument"
        )
        op.drop_index(
            "ix_knowledge_document_title_trgm", table_name="knowledgedocument"
        )
    op.drop_index("ix_knowledge_document_topic", table_name="knowledgedocument")
    op.drop_index(
        "ix_knowledgedocument_source_task_id", table_name="knowledgedocument"
    )
    op.drop_index(
        "ix_knowledgedocument_project_id", table_name="knowledgedocument"
    )
    op.drop_table("knowledgedocument")
