"""artifact_task_id_index_and_parent_fk_on_delete_set_null

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-04 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade():
    # M14: Add missing index on artifact.task_id for efficient cascade deletes and lookups
    op.create_index('ix_artifact_task_id', 'artifact', ['task_id'])

    # M13: Add ON DELETE SET NULL to parent_task_id FK — prevents FK violations
    # when parent tasks are deleted via project cascade
    op.drop_constraint('fk_task_parent_task_id', 'task', type_='foreignkey')
    op.create_foreign_key('fk_task_parent_task_id', 'task', 'task',
                          ['parent_task_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint('fk_task_parent_task_id', 'task', type_='foreignkey')
    op.create_foreign_key('fk_task_parent_task_id', 'task', 'task',
                          ['parent_task_id'], ['id'])

    op.drop_index('ix_artifact_task_id', table_name='artifact')
