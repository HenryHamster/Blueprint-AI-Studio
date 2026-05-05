"""Add regressionremediationcase table for regression class tracking.

Persistent record for regression fingerprints detected across agent runs,
tracking the status and remediation efforts for each regression class
within a project.

Revision ID: 0073
Revises: 0072
Create Date: 2026-05-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = '0073'
down_revision = '0072'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'regressionremediationcase',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('project_id', UUID(as_uuid=True), sa.ForeignKey('project.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('regression_class', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='ACTIVE'),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('sample_message', sa.String(), nullable=True),
        sa.Column('last_remediation_task_id', UUID(as_uuid=True), sa.ForeignKey('task.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint(
        'ux_regressionremediationcase_project_class',
        'regressionremediationcase',
        ['project_id', 'regression_class'],
    )
    op.create_index(
        'ix_regressionremediationcase_project_status_last_seen',
        'regressionremediationcase',
        ['project_id', 'status', 'last_seen_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_regressionremediationcase_project_status_last_seen', table_name='regressionremediationcase')
    op.drop_constraint('ux_regressionremediationcase_project_class', 'regressionremediationcase', type_='unique')
    op.drop_table('regressionremediationcase')
