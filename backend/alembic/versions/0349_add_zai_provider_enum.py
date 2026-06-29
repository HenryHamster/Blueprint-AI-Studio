"""Add Z.AI to the LLM provider enum.

Revision ID: 0349
Revises: 0347
"""

from alembic import op


revision = "0349"
down_revision = "0347"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE llmprovider ADD VALUE IF NOT EXISTS 'zai'")


def downgrade() -> None:
    # PostgreSQL enum value removal is not safe without rebuilding dependent
    # columns; mark this additive enum migration as intentionally irreversible.
    raise NotImplementedError("Downgrading provider enum additions is unsupported")
