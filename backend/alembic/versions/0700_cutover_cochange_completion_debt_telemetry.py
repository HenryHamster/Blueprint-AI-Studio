"""Cut the design_doc write leg for co-change + completion-debt-ledger telemetry.

The God-column decomposition (design 52f97baa) built the dedicated
``project_telemetry`` tables and dual-wrote into them, but the co-change affinity
index (``cochange_index`` / ``cochange_applied_prs``) and the completion-debt
ledger system section (``systems/completion-debt-ledger``) were still fully
rewritten into ``project.design_doc`` on every write. Their writers now persist
SOLELY to ``project_telemetry`` (the design_doc write leg is cut), so this data
migration:

  1. Backfills the merged ``cochange_state`` row (``{"index", "applied_prs"}``)
     from any project that still carries the legacy ``cochange_index`` /
     ``cochange_applied_prs`` design_doc keys but has no ``cochange_state`` row
     yet — so no accumulated affinity data is lost when the fallback stops.
  2. Backfills the ``systems/completion-debt-ledger`` row from the design_doc
     copy where a row does not already exist.
  3. Strips all three keys from every project's ``design_doc`` so the JSONB row
     converges on architectural content alone (the write-serialization + bloat
     relief the decomposition targets).

Backfills use ``ON CONFLICT DO NOTHING`` so the authoritative live-writer row is
never clobbered. No schema changes — ``project_telemetry`` was created by
migration 0659.
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0700"
down_revision = "0699"
branch_labels = None
depends_on = None

_COCHANGE_INDEX_KEY = "cochange_index"
_COCHANGE_APPLIED_KEY = "cochange_applied_prs"
_COCHANGE_STATE_KEY = "cochange_state"
_LEDGER_KEY = "systems/completion-debt-ledger"


def _insert_telemetry(conn, project_id, key: str, value) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO project_telemetry (id, project_id, key, value, updated_at)"
            " VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), :key,"
            " CAST(:value AS jsonb), CURRENT_TIMESTAMP)"
            " ON CONFLICT ON CONSTRAINT uq_project_telemetry_project_key"
            " DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "project_id": project_id,
            "key": key,
            "value": json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
        },
    )


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Backfill the merged cochange_state row from the legacy design_doc keys.
    cochange_rows = conn.execute(
        sa.text(
            "SELECT id,"
            " design_doc -> :idx_key AS idx,"
            " design_doc -> :applied_key AS applied"
            " FROM project"
            " WHERE design_doc ? :idx_key OR design_doc ? :applied_key"
            " ORDER BY id"
        ),
        {"idx_key": _COCHANGE_INDEX_KEY, "applied_key": _COCHANGE_APPLIED_KEY},
    ).fetchall()
    for row in cochange_rows:
        project_id, idx, applied = row[0], row[1], row[2]
        _insert_telemetry(
            conn,
            project_id,
            _COCHANGE_STATE_KEY,
            {"index": idx or {}, "applied_prs": applied or []},
        )

    # 2. Backfill the completion-debt-ledger section row from design_doc.
    ledger_rows = conn.execute(
        sa.text(
            "SELECT id, design_doc -> :key AS value"
            " FROM project"
            " WHERE design_doc ? :key"
            " ORDER BY id"
        ),
        {"key": _LEDGER_KEY},
    ).fetchall()
    for row in ledger_rows:
        project_id, value = row[0], row[1]
        if value is None:
            continue
        _insert_telemetry(conn, project_id, _LEDGER_KEY, value)

    # 3. Strip all three now-migrated keys from every project's design_doc.
    conn.execute(
        sa.text(
            "UPDATE project"
            " SET design_doc = (design_doc - :idx_key - :applied_key - :ledger_key)"
            " WHERE design_doc ? :idx_key"
            "    OR design_doc ? :applied_key"
            "    OR design_doc ? :ledger_key"
        ),
        {
            "idx_key": _COCHANGE_INDEX_KEY,
            "applied_key": _COCHANGE_APPLIED_KEY,
            "ledger_key": _LEDGER_KEY,
        },
    )


def downgrade() -> None:
    # One-way cutover. The telemetry rows may have been created or updated by the
    # live table-only writers after this migration ran, and the schema records no
    # migration provenance — copying them back into design_doc (or deleting them)
    # would risk clobbering authoritative data. Downgrade is intentionally a
    # no-op; the table-preferring readers remain correct either way.
    pass
