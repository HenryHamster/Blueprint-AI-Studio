"""add_alert_table

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'alert',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('severity', sa.String(), nullable=True, server_default='ERROR'),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('message', sa.String(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('dismissed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_alert_project_id', 'alert', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_alert_project_id', table_name='alert')
    op.drop_table('alert')
