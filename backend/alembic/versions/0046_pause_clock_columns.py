"""Add pause-clock columns to deploymentstate

Adds cumulative pause-time counters so any timer in the pipeline can
exclude pipeline-paused intervals from its elapsed-time computation
(CI backstop, queued-age alerts, pre-claim death reaper, etc.).

Two counters split by pause origin so observability can distinguish
self-inflicted (manual) downtime from external-dep-driven (auto). For
timer math, callers always sum the two via ``pause_clock``.

Revision ID: 0046
Revises: 0045
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'deploymentstate',
        sa.Column('total_manual_paused_seconds', sa.Float(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column(
        'deploymentstate',
        sa.Column('total_auto_paused_seconds', sa.Float(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column(
        'deploymentstate',
        sa.Column('paused_kind', sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('deploymentstate', 'paused_kind')
    op.drop_column('deploymentstate', 'total_auto_paused_seconds')
    op.drop_column('deploymentstate', 'total_manual_paused_seconds')
