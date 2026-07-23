"""Backfill remaining design-doc telemetry blobs into project_telemetry.

Seven keys were identified in the final God-column decomposition tranche
(design 52f97baa).  The three dedicated-store keys were already seeded:
  - lane_baselines       → project_lane_baseline (0697)
  - sr_catch_rate_trends → project_sr_analytics  (0696)
  - fix_loop_divergence_alerted_window → project_sr_analytics (0696)
  - direction_first_attempt_feedback   → project_telemetry    (0669 / 0695)

The four remaining generic-KV keys have writers that dual-write to both
``project.design_doc`` and ``project_telemetry``.  This migration backfills
any rows that predate the dual-write cutover so new readers (which prefer
the table with a design_doc fallback) see historical values immediately:
  - repo_overview
  - _broadcast_limit
  - _auto_remediation
  - direction_first_attempt_feedback (idempotent re-backfill; DO NOTHING on conflict)

This is a DATA-only migration — no table schema changes.  The design_doc
copies are intentionally kept (the dual-read/dual-write transition is still
in effect for compatibility with pre-migration serving engines).

Revision ID: 0707
Revises: 0702
Create Date: 2026-07-23
"""

from alembic import op
from sqlalchemy.sql import text

revision = "0707"
down_revision = "0702"
branch_labels = None
depends_on = None

_KV_KEYS = (
    "repo_overview",
    "_broadcast_limit",
    "_auto_remediation",
    "direction_first_attempt_feedback",
)


def upgrade() -> None:
    for key in _KV_KEYS:
        op.execute(
            text(
                """
                INSERT INTO project_telemetry (id, project_id, key, value, updated_at)
                SELECT
                  gen_random_uuid(),
                  p.id,
                  :tk,
                  p.design_doc -> :tk,
                  NOW()
                FROM "project" p
                WHERE p.design_doc ? :tk
                  AND (p.design_doc -> :tk) IS NOT NULL
                ON CONFLICT (project_id, key) DO NOTHING
                """
            ).bindparams(tk=key)
        )


def downgrade() -> None:
    # Forward-only backfill.  Rows for these keys may have been created or
    # updated by live dual-writers after this migration ran; deleting by key
    # alone would destroy authoritative telemetry that this migration did not
    # create.  The legacy design_doc copies remain intact for rollback readers.
    pass
