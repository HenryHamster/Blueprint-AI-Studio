"""Promote Task.llm_account_id from context JSONB to an indexed column.

D2 DB hot-loop over-fetch fix (dir d-57436105). The orchestrator Max-Plan
reconcile loop runs every ~30s and previously issued ``select(Task.context)``
over all PROVISIONING/IN_PROGRESS tasks purely to read a single scalar account
id (``context->>'llm_account_id'`` / legacy ``context->>'max_plan_account_id'``),
TOAST-fetching every active task's full multi-MB context blob each cycle.

This migration adds a dedicated, indexed ``llm_account_id`` column and backfills
it from the JSONB via ``COALESCE(context->>'llm_account_id',
context->>'max_plan_account_id')`` so the reconcile loop can project just the
scalar. The context keys are intentionally left in place (the write path keeps
writing both), so in-flight rows and any rollback remain safe.

Revision ID: 0317
Revises: 0310
"""

import sqlalchemy as sa
from alembic import op


# revision id 0317 is RESERVED for this task (parallel-authoring reservation;
# linearized against production at merge time). down_revision is anchored to the
# current on-disk head (0310) so the chain has a single head locally — the
# reservation note's "so the chain stays linear" goal — and the migration-chain
# gate / `alembic upgrade head` succeed. The merge-time linearizer re-anchors
# down_revision against the production head if it has advanced by then.
revision = "0317"
down_revision = "0310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("llm_account_id", sa.String(), nullable=True),
    )
    # Backfill from the JSONB context, preferring the canonical key and falling
    # back to the legacy max_plan_account_id key for rows written before the
    # rename. NULLIF guards against an empty-string JSON path extraction.
    op.execute(
        """
        UPDATE task
        SET llm_account_id = COALESCE(
            NULLIF(context->>'llm_account_id', ''),
            NULLIF(context->>'max_plan_account_id', '')
        )
        WHERE context ? 'llm_account_id' OR context ? 'max_plan_account_id'
        """
    )
    op.create_index(
        "ix_task_llm_account_id",
        "task",
        ["llm_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_llm_account_id", table_name="task")
    op.drop_column("task", "llm_account_id")
