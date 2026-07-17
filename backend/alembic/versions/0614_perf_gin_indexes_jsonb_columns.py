"""Add GIN indexes on Task.payloads and ProjectMessage.context JSONB columns.

Revision ID: 0614
Revises: 0613
Create Date: 2026-07-17

GIN indexes accelerate all JSONB path queries on these columns, replacing
full sequential scans that occur when filtering on nested fields such as
context['event_type'], context['dedupe_key'], and payloads[0]['type'].
"""

from __future__ import annotations

from alembic import op

revision = "0614"
down_revision = "0613"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_payloads_gin "
            "ON task USING gin (payloads)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_context_gin "
            "ON projectmessage USING gin (context)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_projectmessage_context_gin"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_task_payloads_gin"
        )
