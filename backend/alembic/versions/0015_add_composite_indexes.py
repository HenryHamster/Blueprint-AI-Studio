"""add_composite_indexes

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-02 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_task_status_priority_created', 'task', ['status', 'priority', 'created_at'])
    op.create_index('ix_task_project_status_created', 'task', ['project_id', 'status', 'created_at'])


def downgrade():
    op.drop_index('ix_task_project_status_created', table_name='task')
    op.drop_index('ix_task_status_priority_created', table_name='task')
