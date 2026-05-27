"""Seed build_config.enable_self_improvement=true for is_self_repo projects.

enable_self_improvement now defaults to False (project-config-driven) instead
of inheriting from is_self_repo.  This migration preserves existing behavior by
explicitly setting the flag in build_config for any project where is_self_repo
is true, so those projects continue to have self-improvement enabled.

Revision ID: 0119
Revises: 0118
Create Date: 2026-05-23 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE project
            SET build_config = COALESCE(build_config, '{}'::jsonb)
                             || '{"enable_self_improvement": true}'::jsonb
            WHERE is_self_repo = true
              AND (build_config IS NULL
                   OR (build_config -> 'enable_self_improvement') IS NULL)
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE project
            SET build_config = build_config - 'enable_self_improvement'
            WHERE is_self_repo = true
            """
        )
    )
