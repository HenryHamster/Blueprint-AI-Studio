"""Add audit_defer_count and audit_deferred_system_id to residualremediationcase.

These columns support deferring residual auto-remediation when the mapped
system is in audit mode.

Revision ID: 0056
Revises: 0055
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0056'
down_revision = '0055'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'residualremediationcase',
        sa.Column('audit_defer_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('audit_deferred_system_id', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('residualremediationcase', 'audit_deferred_system_id')
    op.drop_column('residualremediationcase', 'audit_defer_count')
