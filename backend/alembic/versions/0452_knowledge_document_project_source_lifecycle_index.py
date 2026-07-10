"""knowledge_document_project_source_lifecycle_index

Add composite index on (project_id, source_kind, lifecycle_status) to
cover the hot query pattern at knowledge_query.py:677-684 and other
sites that filter on source_kind together with project_id. Without this
index Postgres must sequential-scan within the project's document set
to filter by source_kind.

Revision ID: 0452
Revises: 0451
"""

from alembic import op

revision = "0452"
down_revision = "0451"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_document_project_source_lifecycle",
        "knowledgedocument",
        ["project_id", "source_kind", "lifecycle_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_document_project_source_lifecycle",
        table_name="knowledgedocument",
    )
