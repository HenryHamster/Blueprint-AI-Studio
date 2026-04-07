"""add_vm_runtime_tracking

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('vm_provisioned_at', sa.DateTime(), nullable=True))
    op.add_column('task', sa.Column('vm_started_at', sa.DateTime(), nullable=True))
    op.add_column('task', sa.Column('vm_stopped_at', sa.DateTime(), nullable=True))
    op.add_column('task', sa.Column('vm_spot', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    op.drop_column('task', 'vm_spot')
    op.drop_column('task', 'vm_stopped_at')
    op.drop_column('task', 'vm_started_at')
    op.drop_column('task', 'vm_provisioned_at')
