"""ix_projectmessage_project_message_created

Add composite index on ProjectMessage(project_id, message_type, created_at) to
accelerate the high-signal message fetches in build_operator_attention and
get_operator_message_counts, which filter on (project_id, message_type) and
order by created_at DESC.  Previously only a (project_id, status) index existed,
requiring a filter scan over message_type after the project_id lookup.

Revision ID: 0407
Revises: 0406
"""

from alembic import op


revision = "0407"
down_revision = "0406"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_projectmessage_project_message_created",
        "projectmessage",
        ["project_id", "message_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projectmessage_project_message_created",
        table_name="projectmessage",
    )
