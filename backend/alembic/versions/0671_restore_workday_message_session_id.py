"""Restore persistent Workday conversation identity on project messages.

Revision ID: 0671
Revises: 0670
"""

from alembic import op
import sqlalchemy as sa


revision = "0671"
down_revision = "0670"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectmessage",
        sa.Column("resident_session_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_projectmessage_project_session_created",
        "projectmessage",
        ["project_id", "resident_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projectmessage_project_session_created",
        table_name="projectmessage",
    )
    op.drop_column("projectmessage", "resident_session_id")
