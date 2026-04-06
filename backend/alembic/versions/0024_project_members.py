"""project_members

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-06 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create projectmember table
    op.create_table(
        'projectmember',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False, server_default='member'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_projectmember_project_user', 'projectmember', ['project_id', 'user_id'], unique=True)
    op.create_index('ix_projectmember_user_id', 'projectmember', ['user_id'])

    # 2. Backfill: create owner membership for every existing project
    op.execute("""
        INSERT INTO projectmember (id, project_id, user_id, role, created_at)
        SELECT gen_random_uuid(), id, user_id, 'owner', created_at
        FROM project
    """)

    # 3. Add launched_by_user_id to task table
    op.add_column('task', sa.Column('launched_by_user_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('task', 'launched_by_user_id')
    op.drop_table('projectmember')
