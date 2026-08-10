"""Record the truthful task-origination reading boundary.

Migration 0735 backfilled historical tasks with ``SYSTEM``.  The application
now scopes origination analytics to tasks created after the 2026-08-08 06:59
UTC rollout boundary, when the durable stamping path was live.  This revision
is an immutable schema checkpoint for that application contract; no table
change is required because ``task.created_at`` already supplies the boundary.

Revision ID: 0742
Revises: 0741
Create Date: 2026-08-09
"""

from __future__ import annotations

revision = "0742"
down_revision = "0741"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
