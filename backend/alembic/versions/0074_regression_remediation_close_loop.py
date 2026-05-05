"""Extend regressionremediationcase with close-loop tracking fields.

Adds the columns the dispatcher and task_completion hook need to close the
loop on pipeline-regression remediations: attempts counter, the merged-PR
number/result, two JSONB signal snapshots (at-spawn and at-merge), the
fingerprint→fix audit log, and a needs_manual_intervention_at stamp.

Also adds a CHECK constraint pinning status to the four-state lifecycle
(ACTIVE | MERGED | CLOSED | NEEDS_MANUAL_INTERVENTION).

Revision ID: 0074
Revises: 0073
Create Date: 2026-05-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '0074'
down_revision = '0073'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'regressionremediationcase',
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('last_remediation_pr_number', sa.Integer(), nullable=True),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('last_remediation_result', sa.String(), nullable=True),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('signal_snapshot_at_spawn', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('signal_snapshot_at_merge', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('fingerprint_to_fix_log', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'regressionremediationcase',
        sa.Column('needs_manual_intervention_at', sa.DateTime(), nullable=True),
    )
    op.create_check_constraint(
        'ck_regressionremediationcase_status',
        'regressionremediationcase',
        "status IN ('ACTIVE', 'MERGED', 'CLOSED', 'NEEDS_MANUAL_INTERVENTION')",
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_regressionremediationcase_status',
        'regressionremediationcase',
        type_='check',
    )
    op.drop_column('regressionremediationcase', 'needs_manual_intervention_at')
    op.drop_column('regressionremediationcase', 'fingerprint_to_fix_log')
    op.drop_column('regressionremediationcase', 'signal_snapshot_at_merge')
    op.drop_column('regressionremediationcase', 'signal_snapshot_at_spawn')
    op.drop_column('regressionremediationcase', 'last_remediation_result')
    op.drop_column('regressionremediationcase', 'last_remediation_pr_number')
    op.drop_column('regressionremediationcase', 'attempts')
