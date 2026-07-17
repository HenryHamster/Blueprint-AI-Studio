"""Add expression indexes for ProjectMessage JSONB path predicates.

Revision ID: 0625
Revises: 0624
Create Date: 2026-07-17

The hot paths compare values extracted with ``context->>'event_type'`` and
``context->>'dedupe_key'``.  A default jsonb_ops GIN index does not support
those scalar equality predicates, so index the exact expressions instead.

Task.payloads[0]['type'] is intentionally absent: migration 0238 already
created ``ix_task_payloads_type_all`` on that exact expression.  Adding a
second index would duplicate storage and write amplification without improving
the query plan.
"""

from __future__ import annotations

from alembic import op

revision = "0625"
down_revision = "0624"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_projectmessage_context_event_type "
            "ON projectmessage ((context->>'event_type'))"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_projectmessage_context_dedupe_key "
            "ON projectmessage ((context->>'dedupe_key'))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_projectmessage_context_dedupe_key"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_projectmessage_context_event_type"
        )
