"""knowledge_doc_project_direction_idx

Composite index on KnowledgeDocument(project_id, direction_id) to support
the GROUP BY query in GET /projects/{id}/directions that counts knowledge
documents per direction. Allows an index-only scan instead of a heap scan
over the project's rows.

Revision ID: 0379
Revises: 0378
"""

from alembic import op


revision = "0379"
down_revision = "0378"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_document_project_direction",
        "knowledgedocument",
        ["project_id", "direction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_project_direction",
        table_name="knowledgedocument",
    )
