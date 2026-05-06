"""Add prompt_fingerprint column to task.

Persists the 12-hex prompt digest stamped on every code-change heartbeat so
operators can group recent task outcomes by the assembled-guidance that
drove them. The column was added to ``Task`` in #3805 but the matching
migration was never landed; without this column ``app.routers.agent``'s
heartbeat write path raises UndefinedColumnError on Postgres.

Revision ID: 0078
Revises: 0077
Create Date: 2026-05-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("prompt_fingerprint", sa.String(length=12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "prompt_fingerprint")
