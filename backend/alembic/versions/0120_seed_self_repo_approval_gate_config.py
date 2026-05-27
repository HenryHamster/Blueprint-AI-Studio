"""Seed approval_gate_config for is_self_repo projects.

The approval gate helper is now purely config-driven; code-level fallbacks for
is_self_repo have been removed (task_lifecycle.py). This migration preserves
existing behavior by seeding approval_gate_config with Blueprint's default
infra_files and critical_files for any project where is_self_repo is true and
approval_gate_config is empty or null.

Revision ID: 0120
Revises: 0119
Create Date: 2026-05-23 00:00:00.000000

"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

_BLUEPRINT_DEFAULTS = {
    "infra_files": [
        "backend/app/main.py",
        "backend/app/compute.py",
        "backend/app/models.py",
        "backend/app/database.py",
        "backend/app/security.py",
        "backend/app/auth.py",
        "agent/src/index.ts",
        "CLAUDE.md",
        "backend/promotion_policy.yaml",
    ],
    "critical_files": [
        "backend/app/security.py",
        "backend/app/auth.py",
        "CLAUDE.md",
        "backend/promotion_policy.yaml",
    ],
}

# Pre-rendered JSON for use in SQL literals (all ASCII, no single quotes).
_DEFAULTS_JSON = json.dumps(_BLUEPRINT_DEFAULTS, separators=(",", ":"))


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE project
            SET approval_gate_config = '{_DEFAULTS_JSON}'::jsonb
            WHERE is_self_repo = true
              AND (approval_gate_config IS NULL
                   OR approval_gate_config = '{{}}'::jsonb)
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE project
            SET approval_gate_config = '{{}}'::jsonb
            WHERE is_self_repo = true
              AND approval_gate_config = '{_DEFAULTS_JSON}'::jsonb
            """
        )
    )
