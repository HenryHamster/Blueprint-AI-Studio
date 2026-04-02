"""add_deployment_state_and_is_self_repo

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DeploymentState singleton table for tracking deploy health
    op.create_table(
        'deploymentstate',
        sa.Column('id', sa.Integer(), nullable=False, default=1),
        sa.Column('current_revision', sa.String(), nullable=False, server_default=''),
        sa.Column('current_sha', sa.String(), nullable=False, server_default=''),
        sa.Column('deployed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('status', sa.String(), nullable=False, server_default='STABLE'),
        sa.Column('last_known_good_revision', sa.String(), nullable=False, server_default=''),
        sa.Column('last_known_good_sha', sa.String(), nullable=False, server_default=''),
        sa.Column('rolled_back_from_sha', sa.String(), nullable=True),
        sa.Column('rollback_reason', sa.String(), nullable=True),
        sa.Column('rollback_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pause_reason', sa.String(), nullable=True),
        sa.Column('paused_at', sa.DateTime(), nullable=True),
        sa.Column('pre_pause_max_vms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # is_self_repo flag on Project
    op.add_column('project', sa.Column('is_self_repo', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('project', 'is_self_repo')
    op.drop_table('deploymentstate')
