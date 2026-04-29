"""Add directiondriftsnapshot table for drift time-series tracking.

Revision ID: 0057
Revises: 0056
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0057'
down_revision = '0056'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'directiondriftsnapshot',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('direction_id', sa.String(), nullable=False),
        sa.Column('snapshot_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_changes_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('actually_merged_prs', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('claim_shipped_consistency', sa.Float(), nullable=False, server_default=sa.text('1.0')),
        sa.Column('over_claim_flag', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('stalled_despite_churn_flag', sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_index('ix_directiondriftsnapshot_project_id', 'directiondriftsnapshot', ['project_id'])
    op.create_index('ix_directiondriftsnapshot_direction_id', 'directiondriftsnapshot', ['direction_id'])
    op.create_index(
        'ix_dds_project_direction_at',
        'directiondriftsnapshot',
        ['project_id', 'direction_id', 'snapshot_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_dds_project_direction_at', table_name='directiondriftsnapshot')
    op.drop_index('ix_directiondriftsnapshot_direction_id', table_name='directiondriftsnapshot')
    op.drop_index('ix_directiondriftsnapshot_project_id', table_name='directiondriftsnapshot')
    op.drop_table('directiondriftsnapshot')
