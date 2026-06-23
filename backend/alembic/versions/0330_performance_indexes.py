"""Add performance indexes on Alert.severity and ProjectMessage.created_at.

Adds four indexes to support time-windowed and severity-filtered queries
without full table scans:
- ix_alert_severity on alert(severity)
- ix_alert_project_severity on alert(project_id, severity)
- ix_projectmessage_created_at on projectmessage(created_at)
- ix_projectmessage_project_created_at on projectmessage(project_id, created_at)

Revision ID: 0330
Revises: 0329

NOTE: revision id 0309 is reserved for the DEPLOYED computejob heartbeat
migration (applied + immutable on main).  These performance indexes were
originally authored at 0309 on the integration branch, colliding with that
deployed id; they are re-chained here at the head (0330, after 0329 merge_grade)
so the Alembic graph stays linear and ``alembic upgrade head`` applies them
fresh on every environment that already has the deployed 0309 heartbeat.
"""

from alembic import op


revision = "0330"
down_revision = "0329"
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
