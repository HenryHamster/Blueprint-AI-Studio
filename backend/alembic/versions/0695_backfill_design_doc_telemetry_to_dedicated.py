"""Backfill migrated telemetry blobs from project.design_doc to project_telemetry.

Migration 0658/0659 introduced the project_telemetry KV table and dual-write
pattern for telemetry blobs that had accreted in the project.design_doc JSONB
column.  Seven transitional blobs were being dual-written to both stores; this
migration backfills any existing data from design_doc (the legacy location) into
project_telemetry so that after the code cutover (which stops the design_doc
write leg entirely) no data is lost for projects that existed before the
migration was applied.

Affected keys:
  - cochange_index / cochange_applied_prs → "cochange_state" (merged compound key)
  - module_feedback
  - retired_diagnoses_archive
  - playbook_archive
  - proposed_changes
  - direction_first_attempt_feedback
  - systems/completion-debt-ledger

Revision ID: 0695
Revises: 0694
Create Date: 2026-07-23
"""

from alembic import op
from sqlalchemy.sql import text

revision = "0695"
down_revision = "0694"
branch_labels = None
depends_on = None


def _backfill_compound_key(
    op,
    telemetry_key: str,
    doc_keys: list[str],
    value_expr: str,
) -> None:
    """Insert a compound telemetry row built from one or more design_doc fields.

    ``value_expr`` is a raw SQL expression that references the ``design_doc``
    JSONB column, e.g. ``'jsonb_build_object(...)'``.
    """
    doc_keys_clause = " AND ".join(
        f"p.design_doc ? :k{i}" for i, _ in enumerate(doc_keys)
    )
    params = {f"k{i}": k for i, k in enumerate(doc_keys)}
    op.execute(
        text(
            f"""
            INSERT INTO project_telemetry (id, project_id, key, value, updated_at)
            SELECT
              gen_random_uuid(),
              p.id,
              :tk,
              {value_expr},
              NOW()
            FROM "project" p
            WHERE {doc_keys_clause}
            ON CONFLICT (project_id, key) DO NOTHING
            """
        ).bindparams(tk=telemetry_key, **params)
    )


def _backfill_simple_key(op, telemetry_key: str) -> None:
    """Insert a telemetry row for a single design_doc key that maps 1:1."""
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
            ON CONFLICT (project_id, key) DO NOTHING
            """
        ).bindparams(tk=telemetry_key)
    )


def upgrade():
    # 1. Compound key: cochange_index + cochange_applied_prs → "cochange_state"
    _backfill_compound_key(
        op,
        telemetry_key="cochange_state",
        doc_keys=["cochange_index", "cochange_applied_prs"],
        value_expr=(
            "jsonb_build_object("
            "'index', p.design_doc -> 'cochange_index', "
            "'applied_prs', p.design_doc -> 'cochange_applied_prs'"
            ")"
        ),
    )

    # 2-7. Simple 1:1 keys
    for key in (
        "module_feedback",
        "retired_diagnoses_archive",
        "playbook_archive",
        "proposed_changes",
        "direction_first_attempt_feedback",
        "systems/completion-debt-ledger",
    ):
        _backfill_simple_key(op, key)


def downgrade():
    # Remove the backfilled rows for the migrated keys.
    op.execute(
        text(
            """
            DELETE FROM project_telemetry
            WHERE key IN (
              'cochange_state',
              'module_feedback',
              'retired_diagnoses_archive',
              'playbook_archive',
              'proposed_changes',
              'direction_first_attempt_feedback',
              'systems/completion-debt-ledger'
            )
            """
        )
    )
