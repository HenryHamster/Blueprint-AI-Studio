"""add_alert_task_dismissed_index

Adds composite index on Alert(task_id, dismissed_at) to back the hot-path
lookup `Alert.task_id == X AND Alert.dismissed_at IS NULL` used on the
task-failure path.

Revision ID: 0036
Revises: 0035
Create Date: 2026-04-20 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_alert_task_id_dismissed',
        'alert',
        ['task_id', 'dismissed_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_alert_task_id_dismissed', table_name='alert')
