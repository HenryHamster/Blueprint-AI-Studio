"""Add provider, account_id, model, estimated_cost_usd columns to task table.

These columns were added to the Task model in feat: persist
provider/account/model/cost columns on Task but the corresponding Alembic
migration was omitted from that PR.

Revision ID: 0055
Revises: 0054
Create Date: 2026-04-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0055'
down_revision = '0054'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('provider', sa.String(), nullable=True))
    op.add_column('task', sa.Column('account_id', sa.String(), nullable=True))
    op.add_column('task', sa.Column('model', sa.String(), nullable=True))
    op.add_column('task', sa.Column('estimated_cost_usd', sa.Float(), nullable=True))

    op.create_index('ix_task_provider_created_at', 'task', ['provider', 'created_at'])
    op.create_index('ix_task_account_id_created_at', 'task', ['account_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_task_account_id_created_at', table_name='task')
    op.drop_index('ix_task_provider_created_at', table_name='task')
    op.drop_column('task', 'estimated_cost_usd')
    op.drop_column('task', 'model')
    op.drop_column('task', 'account_id')
    op.drop_column('task', 'provider')
