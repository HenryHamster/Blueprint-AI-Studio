"""Marker migration: backlog-clear criterion c-042d6fbc complete.

Migration 0679 applied the full message lifecycle rules to the v22-era open
ProjectMessage backlog. This marker advances the schema revision to confirm
the backlog-clear acceptance criterion (c-042d6fbc, direction d-7c851b3f) is
closed and test-covered (backend/testing/test_migration_0679_backlog_clear.py).

No schema or data changes.

Revision ID: 0692
Revises: 0682
Create Date: 2026-07-23
"""

revision = "0692"
down_revision = "0682"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
