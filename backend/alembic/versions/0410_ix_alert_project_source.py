"""ix_alert_project_source

Add composite index on (project_id, source) for Alert to speed up
source-filtered queries in admin_notifications.py.

Revision ID: 0410
Revises: 0409
"""

from alembic import op

revision = "0410"
down_revision = "0409"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_alert_project_source", "alert", ["project_id", "source"])


def downgrade() -> None:
    op.drop_index("ix_alert_project_source", table_name="alert")
