"""add_account_usage_caps

Add usage-cap and rolling-daily-counter fields to maxplanaccount so the
orchestrator can throttle per account: a daily task cap, a max-concurrent cap,
and auto-disable after N consecutive rate-limit hits. disabled_reason lets the
UI tell users why an auto-disabled account went dark.

Revision ID: 0040
Revises: 0039
Create Date: 2026-04-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("maxplanaccount", sa.Column("daily_task_cap", sa.Integer(), nullable=True))
    op.add_column("maxplanaccount", sa.Column("max_concurrent_tasks", sa.Integer(), nullable=True))
    op.add_column("maxplanaccount", sa.Column("auto_disable_after_rate_limits", sa.Integer(), nullable=True))
    op.add_column(
        "maxplanaccount",
        sa.Column("daily_task_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("maxplanaccount", sa.Column("daily_task_count_reset_at", sa.DateTime(), nullable=True))
    op.add_column("maxplanaccount", sa.Column("disabled_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("maxplanaccount", "disabled_reason")
    op.drop_column("maxplanaccount", "daily_task_count_reset_at")
    op.drop_column("maxplanaccount", "daily_task_count")
    op.drop_column("maxplanaccount", "auto_disable_after_rate_limits")
    op.drop_column("maxplanaccount", "max_concurrent_tasks")
    op.drop_column("maxplanaccount", "daily_task_cap")
