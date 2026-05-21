"""Add composite index on Alert(project_id, dismissed_at) for per-project alert lookup.

Supports the common query pattern WHERE project_id = ? AND dismissed_at IS NULL
used in _create_halted_recovery_alerts and _create_rollback_alerts.

Revision ID: 0106
Revises: 0105
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_alert_project_id_dismissed",
        "alert",
        ["project_id", "dismissed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_project_id_dismissed", table_name="alert")
