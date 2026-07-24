"""Add expression index on projectmessage context->>'audience'.

Revision ID: 0656
Revises: 0655
Create Date: 2026-07-21

The operator_visible_audience_filter() in message_audience.py uses
context->>'audience' as a scalar predicate in 10+ hot query paths
(operator_attention.py, operator_inbox_liveness.py). A functional
expression index accelerates those queries over the sequential scan.
"""

from __future__ import annotations

from alembic import op

revision = "0656"
down_revision = "0655"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_projectmessage_context_audience "
            "ON projectmessage ((context->>'audience'))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_projectmessage_context_audience"
        )
