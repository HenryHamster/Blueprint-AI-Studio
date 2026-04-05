"""add_global_autonomous_limit_and_direction_cooldown

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('deploymentstate', sa.Column('runtime_max_global_autonomous_tasks_per_hour', sa.Integer(), nullable=True))
    op.add_column('deploymentstate', sa.Column('runtime_autonomous_direction_cooldown_minutes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('deploymentstate', 'runtime_autonomous_direction_cooldown_minutes')
    op.drop_column('deploymentstate', 'runtime_max_global_autonomous_tasks_per_hour')
