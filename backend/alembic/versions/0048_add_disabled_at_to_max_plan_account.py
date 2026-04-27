"""Persist disabled_at on MaxPlanAccount so auto-disabled accounts can
auto-recover after orchestrator restart.

Audit 2026-04-25 finding E7: max_plan_pool's recheck_disabled_accounts()
re-enables accounts only when ``disabled_at`` is non-null. Auto-disable
sets ``disabled_at`` in-memory but the column didn't exist, so on
orchestrator restart the in-memory ``disabled_at`` was lost and recheck
skipped the account forever.

Backfills disabled_at for already-disabled accounts using updated_at as
a best-effort estimate so they participate in the next recheck cycle
instead of staying stuck.

Revision ID: 0048
Revises: 0047
Create Date: 2026-04-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'maxplanaccount',
        sa.Column('disabled_at', sa.DateTime(timezone=False), nullable=True),
    )
    op.execute(
        "UPDATE maxplanaccount SET disabled_at = updated_at "
        "WHERE enabled = FALSE AND disabled_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column('maxplanaccount', 'disabled_at')
