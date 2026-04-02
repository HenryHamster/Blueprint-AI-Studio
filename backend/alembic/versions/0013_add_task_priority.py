"""add_task_priority

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('priority', sa.Integer(), nullable=False, server_default=sa.text("50")))
    op.create_index('ix_task_status_priority', 'task', ['status', 'priority'])


def downgrade() -> None:
    op.drop_index('ix_task_status_priority', table_name='task')
    op.drop_column('task', 'priority')
