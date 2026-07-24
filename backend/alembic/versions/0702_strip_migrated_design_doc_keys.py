"""Strip remaining telemetry-migrated keys from project.design_doc.

The God-column decomposition (design 52f97baa) built the dedicated
``project_telemetry`` KV table and backfilled the following keys from
``project.design_doc`` into it (migration 0695).  Code-side writers
already persist SOLELY to ``project_telemetry`` (``strip_migrated_doc_keys``
removes them from the in-memory dict before every ``flag_modified`` write).
No data is lost — the project_telemetry table is the canonical store.

Three keys from the original set (``cochange_index``,
``cochange_applied_prs``, ``systems/completion-debt-ledger``) were already
stripped by migration 0700.  This migration strips the remainder:

  - module_feedback
  - retired_diagnoses_archive
  - playbook_archive
  - proposed_changes
  - direction_first_attempt_feedback

No schema changes — ``project_telemetry`` was created by migration 0659,
and all five keys were backfilled by migration 0695.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0702"
down_revision = "0701"
branch_labels = None
depends_on = None

_TARGET_KEYS = (
    "module_feedback",
    "retired_diagnoses_archive",
    "playbook_archive",
    "proposed_changes",
    "direction_first_attempt_feedback",
)


def upgrade() -> None:
    conn = op.get_bind()

    # Build a parameterised ``-`` expression so the WHERE clause can use a
    # single ``?`` (key existence) check per project rather than N separate
    # UPDATEs.  This is an indexed JSONB operation and runs efficiently even
    # on large project tables.
    for key in _TARGET_KEYS:
        conn.execute(
            sa.text(
                "UPDATE project"
                " SET design_doc = design_doc - :key"
                " WHERE design_doc ? :key"
            ),
            {"key": key},
        )


def downgrade() -> None:
    # No downgrade: the migrated keys are still in ``project_telemetry``
    # and the runtime ``read_telemetry_or_doc`` / ``get_project_telemetry``
    # helpers handle row-absence by returning ``None`` (or the caller's
    # default), so a rollback of this migration is safe.  Re-inserting the
    # stripped values would require a full backfill replay.
    pass
