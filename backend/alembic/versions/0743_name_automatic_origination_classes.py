"""Checkpoint the complete automatic task-origination taxonomy.

The origination column is already a nullable-free ``String(32)`` and the new
classes require no table change. This immutable checkpoint records that the
spawn-time taxonomy is now complete and keeps deployed schema checks aligned.

Revision ID: 0743
Revises: 0742
Create Date: 2026-08-09
"""

from __future__ import annotations

revision = "0743"
down_revision = "0742"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
