"""persist_runtime_config

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('deploymentstate', sa.Column('runtime_max_concurrent_vms', sa.Integer(), nullable=True))
    op.add_column('deploymentstate', sa.Column('runtime_max_retries', sa.Integer(), nullable=True))
    op.add_column('deploymentstate', sa.Column('runtime_max_autonomous_tasks_per_hour', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('deploymentstate', 'runtime_max_autonomous_tasks_per_hour')
    op.drop_column('deploymentstate', 'runtime_max_retries')
    op.drop_column('deploymentstate', 'runtime_max_concurrent_vms')
