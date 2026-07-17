"""Add composite index on ProjectMessage(project_id, status, created_at DESC) for live-message preview.

Revision ID: 0624
Revises: 0623
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "0624"
down_revision = "0623"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Accelerates the strategic cold-load live-message preview query that
        # filters by (project_id, status IN ('open','linked')) and orders by
        # created_at DESC LIMIT 12.  The two-branch UNION ALL rewrite uses this
        # index to serve each branch as an index-only scan.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_projectmessage_project_status_created "
            "ON projectmessage (project_id, status, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS ix_projectmessage_project_status_created"
        )
