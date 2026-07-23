"""Enable walkthrough_e2e lane for Blueprint project via build_config.

Revision ID: 0701
Revises: 0700
Create Date: 2026-07-23

Sets build_config['walkthrough_e2e_enabled'] = true for the Blueprint
self-repo project (github_repo = 'blueprintaistudio'), so autonomous
qualification runs produce walkthrough_e2e evidence without requiring a
runtime_config override.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0701"
down_revision = "0700"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE project
            SET build_config = COALESCE(build_config, '{}'::jsonb)
                               || '{"walkthrough_e2e_enabled": true}'::jsonb
            WHERE LOWER(github_repo) = 'blueprintaistudio'
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE project
            SET build_config = build_config - 'walkthrough_e2e_enabled'
            WHERE LOWER(github_repo) = 'blueprintaistudio'
            """
        )
    )
