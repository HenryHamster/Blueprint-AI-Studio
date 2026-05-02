"""Add heartbeat_count column to Task.

R3-W1: Round 3 audit fix C1. The heartbeat audit-trail gate counter
was stored in task.context['_heartbeat_count'] (Round 2 F2 fix), but
that triggered full JSONB rewrites every heartbeat — the exact failure
mode B2 was created to fix. Move to a dedicated Integer column so the
storage layer can do in-place column updates without TOAST page churn.

Revision ID: 0066
Revises: 0065
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column(
            "heartbeat_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("task", "heartbeat_count")
