"""Add task hierarchy and history

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-19 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Add new columns to task
    op.add_column('task', sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='Untitled Task'))
    op.add_column('task', sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
    op.add_column('task', sa.Column('parent_task_id', sqlmodel.sql.sqltypes.GUID(), nullable=True))
    op.add_column('task', sa.Column('latest_status_message', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('task', sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='USER'))
    op.add_column('task', sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default='false'))
    
    op.create_foreign_key('fk_task_parent_task_id', 'task', 'task', ['parent_task_id'], ['id'])
    op.create_index(op.f('ix_task_parent_task_id'), 'task', ['parent_task_id'], unique=False)

    # Create taskhistory table
    op.create_table('taskhistory',
    sa.Column('id', sqlmodel.sql.sqltypes.GUID(), nullable=False),
    sa.Column('task_id', sqlmodel.sql.sqltypes.GUID(), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['task.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_taskhistory_task_id'), 'taskhistory', ['task_id'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_taskhistory_task_id'), table_name='taskhistory')
    op.drop_table('taskhistory')
    
    op.drop_index(op.f('ix_task_parent_task_id'), table_name='task')
    op.drop_constraint('fk_task_parent_task_id', 'task', type_='foreignkey')
    
    op.drop_column('task', 'requires_approval')
    op.drop_column('task', 'created_by')
    op.drop_column('task', 'latest_status_message')
    op.drop_column('task', 'parent_task_id')
    op.drop_column('task', 'description')
    op.drop_column('task', 'title')
