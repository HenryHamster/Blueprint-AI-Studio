"""Add compound (project_id, created_at) index on task table.

This index makes the time-range scan in drift_detection.compute_direction_drift
efficient on large projects where the function is called on every
autonomous_research tick and strategic_review spawn.

Revision ID: 0053
Revises: 0052
Create Date: 2026-04-28 00:00:00.000000

"""
from alembic import op


revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_task_project_id_created_at',
        'task',
        ['project_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_task_project_id_created_at', table_name='task')
