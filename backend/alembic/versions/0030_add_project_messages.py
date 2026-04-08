"""add_project_messages

Async message board between users and the strategic agent.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'projectmessage',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('message_type', sa.String(), server_default='NOTE', nullable=True),
        sa.Column('from_type', sa.String(), server_default='user', nullable=False),
        sa.Column('from_task_id', sa.Uuid(), nullable=True),
        sa.Column('content', sa.String(), nullable=False, server_default=''),
        sa.Column('context', JSONB, nullable=True, server_default='{}'),
        sa.Column('status', sa.String(), server_default='open', nullable=False),
        sa.Column('resolution', sa.String(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['from_task_id'], ['task.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projectmessage_project_id', 'projectmessage', ['project_id'])
    op.create_index('ix_projectmessage_project_status', 'projectmessage', ['project_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_projectmessage_project_status', 'projectmessage')
    op.drop_index('ix_projectmessage_project_id', 'projectmessage')
    op.drop_table('projectmessage')
