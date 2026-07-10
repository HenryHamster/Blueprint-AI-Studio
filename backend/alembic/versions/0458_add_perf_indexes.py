"""add performance indexes for alert, computejob, and knowledge_document

Revision ID: 0458
Revises: 0457
"""

from alembic import op

revision = "0458"
down_revision = "0457"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_alert_project_created_at",
        "alert",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_computejob_project_status_created",
        "computejob",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_knowledge_document_project_lifecycle_created",
        "knowledgedocument",
        ["project_id", "lifecycle_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_document_project_lifecycle_created", table_name="knowledgedocument")
    op.drop_index("ix_computejob_project_status_created", table_name="computejob")
    op.drop_index("ix_alert_project_created_at", table_name="alert")
