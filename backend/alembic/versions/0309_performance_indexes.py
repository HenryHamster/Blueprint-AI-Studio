"""Add performance indexes on Alert.severity and ProjectMessage.created_at.

Adds four indexes to support time-windowed and severity-filtered queries
without full table scans:
- ix_alert_severity on alert(severity)
- ix_alert_project_severity on alert(project_id, severity)
- ix_projectmessage_created_at on projectmessage(created_at)
- ix_projectmessage_project_created_at on projectmessage(project_id, created_at)

Revision ID: 0309
Revises: 0308
"""

from alembic import op


revision = "0309"
down_revision = "0308"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_alert_severity", "alert", ["severity"], unique=False)
    op.create_index(
        "ix_alert_project_severity", "alert", ["project_id", "severity"], unique=False
    )
    op.create_index(
        "ix_projectmessage_created_at", "projectmessage", ["created_at"], unique=False
    )
    op.create_index(
        "ix_projectmessage_project_created_at",
        "projectmessage",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_projectmessage_project_created_at", table_name="projectmessage")
    op.drop_index("ix_projectmessage_created_at", table_name="projectmessage")
    op.drop_index("ix_alert_project_severity", table_name="alert")
    op.drop_index("ix_alert_severity", table_name="alert")
