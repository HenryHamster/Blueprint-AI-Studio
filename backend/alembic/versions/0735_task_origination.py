"""Add durable task-origination class.

Adds the schema backing ``Task.origination``, a durable record of WHY a task
was created, distinct from ``Task.created_by`` (which collapses every automatic
spawn — research, rollup, review, hotfix backmerge — into the single
``ORCHESTRATOR`` value). Merged-work analytics (``merged_work_origination_split``)
read this column to attribute each landed PR to its origin without walking
parent lineage.

* One non-null ``task`` column with a constant server default. The default is a
  constant ``'SYSTEM'`` string, so the ADD COLUMN is metadata-only on
  PostgreSQL 11+ and existing rows are backfilled to SYSTEM (ordinary
  orchestration) — they are never research- or strategist-attributed.

Per CLAUDE.md this migration is chained from the latest head (0728,
``inline_llm_spend_pricing_status``) and ``EXPECTED_SCHEMA_REVISION`` is bumped
to 0735 to match.

Revision ID: 0735
Revises: 0728
Create Date: 2026-08-07

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0735"
down_revision = "0728"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column(
            "origination",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'SYSTEM'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("task", "origination")
