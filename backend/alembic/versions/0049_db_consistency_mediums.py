"""DB consistency medium audit fixes

Revision ID: 0049
Revises: 0048
Create Date: 2026-04-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ux_task_active_idempotency_key",
        "task",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN "
            "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING')"
        ),
    )
    op.create_unique_constraint(
        "uq_userintegration_user_id",
        "userintegration",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_userintegration_user_id",
        "userintegration",
        type_="unique",
    )
    op.drop_index("ux_task_active_idempotency_key", table_name="task")
    op.drop_column("task", "idempotency_key")
