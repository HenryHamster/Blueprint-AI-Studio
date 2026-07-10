"""maxplanaccount_per_project_caps

Add per-project cap and state columns to MaxPlanAccount so per-project
pool tracking can persist across restarts. All three are nullable JSONB
columns — a null value means "no per-project caps configured" and the
global caps apply.

- project_daily_task_cap: JSONB dict {project_id: int}
- project_max_concurrent_tasks: JSONB dict {project_id: int}
- project_state: JSONB dict {project_id: {active_tasks: int, daily_task_count: int}}

Revision ID: 0471
Revises: 0470
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0471"
down_revision = "0470"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "maxplanaccount",
        sa.Column("project_daily_task_cap", JSONB, nullable=True),
    )
    op.add_column(
        "maxplanaccount",
        sa.Column("project_max_concurrent_tasks", JSONB, nullable=True),
    )
    op.add_column(
        "maxplanaccount",
        sa.Column("project_state", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("maxplanaccount", "project_state")
    op.drop_column("maxplanaccount", "project_max_concurrent_tasks")
    op.drop_column("maxplanaccount", "project_daily_task_cap")
