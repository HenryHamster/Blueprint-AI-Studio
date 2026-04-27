"""Add residualremediationcase table for CODE_ERROR fingerprint tracking

Tracks residual CODE_ERROR fingerprints with support for:
- CLOSED_ORGANICALLY: auto-closed by reaper after 72 h of silence
- IGNORED: admin-suppressed, skipped by auto-remediation spawn

Revision ID: 0051
Revises: 0050
Create Date: 2026-04-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'residualremediationcase',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('project_id', UUID(as_uuid=True), sa.ForeignKey('project.id'), nullable=True, index=True),
        sa.Column('fingerprint', sa.String(), nullable=False, index=True),
        sa.Column('status', sa.String(), nullable=False, server_default='ACTIVE'),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('sample_message', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index(
        'ix_residual_case_status_last_seen',
        'residualremediationcase',
        ['status', 'last_seen_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_residual_case_status_last_seen', table_name='residualremediationcase')
    op.drop_table('residualremediationcase')
