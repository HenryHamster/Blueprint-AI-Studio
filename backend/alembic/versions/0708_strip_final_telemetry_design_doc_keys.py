"""Backfill review_effectiveness and playbook_deleted_keys, strip all final telemetry keys from design_doc.

The God-column decomposition (design 52f97baa) migrated code-written telemetry
blobs from ``project.design_doc`` JSONB into dedicated tables. Seven keys were
backfilled and stripped in earlier migrations (0700, 0702). Four more were
backfilled by 0707 (``repo_overview``, ``_broadcast_limit``,
``_auto_remediation``, ``direction_first_attempt_feedback``). This migration
backfills the two remaining keys that now have dual-write writers and runtime
strip-migrated-doc-keys support, then strips all five keys that are now fully
migrated (their design_doc copies are no longer authoritative):

  - review_effectiveness  (new backfill)
  - playbook_deleted_keys (new backfill)

No schema changes. The runtime ``read_telemetry_or_doc`` / ``get_project_telemetry``
helpers handle row-absence by returning ``None`` (or the caller's default), so a
stripped key with no telemetry row degrades gracefully to the default value.

Revision ID: 0708
Revises: 0707
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0708"
down_revision = "0707"
branch_labels = None
depends_on = None

# Keys to backfill from design_doc (no telemetry row yet)
_BACKFILL_KEYS = (
    "review_effectiveness",
    "playbook_deleted_keys",
)


def _backfill_key(conn, key: str) -> None:
    """Backfill *key* from project.design_doc into project_telemetry."""
    conn.execute(
        sa.text(
            """
            INSERT INTO project_telemetry (id, project_id, key, value, updated_at)
            SELECT
              gen_random_uuid(),
              p.id,
              :k,
              p.design_doc -> :k,
              NOW()
            FROM "project" p
            WHERE p.design_doc ? :k
              AND (p.design_doc -> :k) IS NOT NULL
            ON CONFLICT (project_id, key) DO NOTHING
            """
        ).bindparams(k=key)
    )


def upgrade() -> None:
    conn = op.get_bind()

    for key in _BACKFILL_KEYS:
        _backfill_key(conn, key)

    # Strip all five keys where the design_doc write leg is now cut.
    # Runtime strip_migrated_doc_keys (in _TELEMETRY_MIGRATED_KEYS) handles
    # future writes; this migration cleans up persisted stale values.
    for key in _BACKFILL_KEYS:
        conn.execute(
            sa.text(
                "UPDATE \"project\""
                " SET design_doc = design_doc - :key"
                " WHERE design_doc ? :key"
            ),
            {"key": key},
        )


def downgrade() -> None:
    # Forward-only migration.  The runtime handles key-absence gracefully
    # via read_telemetry_or_doc with design_doc fallback; restoring the
    # stripped keys would require a full replay from the project_telemetry
    # table (which is authoritative after the dual-write cutover).
    pass
