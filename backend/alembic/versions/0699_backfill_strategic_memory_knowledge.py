"""Backfill legacy strategic_memory_notes into KnowledgeDocument rows.

Revision ID: 0699
Revises: 0698
Create Date: 2026-07-23

Data migration (design 2812f1ed, completing the KnowledgeDocument cutover for
strategic memory). ``set_memory`` now routes every behavioral fact to a
``KnowledgeDocument`` row (``source_kind='strategic_memory'``), and both reader
endpoints (admin strategic-state, agent section-fetch) read the
``build_strategic_memory_index()`` KnowledgeDocument view. But notes written
before migration 0680 exist ONLY in the ``Project.strategic_memory_notes`` JSONB
list — they carry no ``doc_id`` and are invisible to the index builder. This
migration creates one KnowledgeDocument row per un-migrated legacy entry so the
pre-existing memory is retrievable via ``./fetch knowledge --source-kind
strategic_memory`` and appears in the strategist spawn payload / checkpoint
state.

Idempotency: each backfilled row is keyed by a DETERMINISTIC uuid5 derived from
(project_id, set_at, note), so re-running the migration re-derives the same id
and skips rows that already exist. Entries that already carry a ``doc_id``
pointing at an existing KnowledgeDocument row (written after 0680) are skipped
outright — they were never legacy-only.

The ``strategic_memory_notes`` JSONB column is intentionally left in place for
rollback safety; it simply stops receiving new writes (the dual-write leg is cut
in ``admin_agent_ops.py``). No schema change.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0699"
down_revision = "0698"
branch_labels = None
depends_on = None

# ``source_kind`` value for strategic-memory facts (SourceKind.STRATEGIC_MEMORY).
# Hardcoded so the migration stays decoupled from the app enum.
_STRATEGIC_MEMORY = "strategic_memory"

# Fixed namespace for the deterministic uuid5 backfill ids. Any stable UUID
# works; this one is dedicated to this backfill so ids cannot collide with other
# uuid5 usages.
_BACKFILL_NAMESPACE = uuid.UUID("6f3a1e2c-0b9d-4a7e-9c11-5b7e2a10f0c1")

_MARKER_STRATEGY = "backfill"


# Lightweight table projections. Explicit ``sa.Uuid`` / ``sa.JSON`` types match
# the SQLModel column mappings exactly, so values written here round-trip through
# the ORM read paths byte-for-byte (critical for the uuid storage format on
# SQLite, where a format mismatch would break project-scoped WHERE filters).
def _knowledgedocument_table() -> sa.Table:
    return sa.Table(
        "knowledgedocument",
        sa.MetaData(),
        sa.Column("id", sa.Uuid()),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("source_task_id", sa.Uuid(), nullable=True),
        sa.Column("source_kind", sa.String()),
        sa.Column("title", sa.String()),
        sa.Column("summary", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("tags", sa.JSON()),
        sa.Column("source_citations", sa.JSON()),
        sa.Column("forward_quality_verdict", sa.JSON()),
        sa.Column("lifecycle_status", sa.String()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def _parse_set_at(value: object) -> datetime:
    """Parse an entry's ``set_at`` ISO timestamp into a naive-UTC datetime.

    The KnowledgeDocument ``created_at`` column is naive-UTC (NaiveUTCDateTime).
    Falls back to the current time when the field is missing or unparseable.
    """
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _collapse(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def upgrade() -> None:
    conn = op.get_bind()
    kd = _knowledgedocument_table()

    # Read every project that carries a non-empty legacy note list. A raw text()
    # select keeps this dialect-agnostic: sync psycopg auto-parses jsonb to a
    # Python list, while SQLite returns the stored JSON string (normalized below).
    project_rows = conn.execute(
        sa.text(
            "SELECT id, strategic_memory_notes FROM project"
            " WHERE strategic_memory_notes IS NOT NULL"
        )
    ).fetchall()

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for project_row in project_rows:
        project_id = uuid.UUID(str(project_row[0]))
        notes = project_row[1]
        if isinstance(notes, (bytes, bytearray)):
            notes = notes.decode("utf-8")
        if isinstance(notes, str):
            try:
                notes = json.loads(notes)
            except (ValueError, TypeError):
                notes = None
        if not isinstance(notes, list) or not notes:
            continue

        # Ids that already exist for this project — both post-0680 rows and any
        # rows a previous run of this migration created (idempotency).
        existing_ids = {
            str(r[0])
            for r in conn.execute(
                sa.select(kd.c.id).where(kd.c.project_id == project_id)
            ).fetchall()
        }

        pending: list[dict] = []
        for entry in notes:
            if not isinstance(entry, dict):
                continue
            note = entry.get("note")
            if not isinstance(note, str) or not note.strip():
                continue
            note = note.strip()

            # Post-0680 entries carry a doc_id. If that row already exists it was
            # never legacy-only; skip it.
            doc_id_field = entry.get("doc_id")
            if doc_id_field and str(doc_id_field) in existing_ids:
                continue

            set_at = entry.get("set_at")
            new_id = uuid.uuid5(
                _BACKFILL_NAMESPACE,
                f"{project_id}|{set_at}|{note}",
            )
            if str(new_id) in existing_ids:
                continue
            existing_ids.add(str(new_id))

            created_at = _parse_set_at(set_at)
            pending.append(
                {
                    "id": new_id,
                    "project_id": project_id,
                    "source_task_id": None,
                    "source_kind": _STRATEGIC_MEMORY,
                    "title": _collapse(note, 120),
                    "summary": _collapse(note, 280),
                    "body": note,
                    "topic": None,
                    "tags": [],
                    "source_citations": [
                        {
                            "note": "backfilled from legacy strategic_memory_notes",
                            "original_set_at": set_at,
                        }
                    ],
                    "forward_quality_verdict": {
                        "passed": True,
                        "strategy": _MARKER_STRATEGY,
                    },
                    "lifecycle_status": "consumed",
                    "created_at": created_at,
                    "updated_at": now,
                }
            )

        if pending:
            conn.execute(kd.insert(), pending)


def downgrade() -> None:
    # Precisely reversible: delete ONLY the rows this migration created, keyed by
    # the distinctive backfill marker in forward_quality_verdict.strategy. Rows
    # written by the live set_memory op carry strategy='agent_self_attested' and
    # are never touched. On SQLite the JSON is stored as text, so match on both
    # the parsed-PG shape and the serialized-text shape.
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.execute(
            sa.text(
                "DELETE FROM knowledgedocument"
                " WHERE source_kind = :sk"
                " AND forward_quality_verdict ->> 'strategy' = :strategy"
            ),
            {"sk": _STRATEGIC_MEMORY, "strategy": _MARKER_STRATEGY},
        )
    else:
        rows = conn.execute(
            sa.text(
                "SELECT id, forward_quality_verdict FROM knowledgedocument"
                " WHERE source_kind = :sk"
            ),
            {"sk": _STRATEGIC_MEMORY},
        ).fetchall()
        to_delete = []
        for row in rows:
            verdict = row[1]
            if isinstance(verdict, (bytes, bytearray)):
                verdict = verdict.decode("utf-8")
            if isinstance(verdict, str):
                try:
                    verdict = json.loads(verdict)
                except (ValueError, TypeError):
                    verdict = None
            if isinstance(verdict, dict) and verdict.get("strategy") == _MARKER_STRATEGY:
                to_delete.append(str(row[0]))
        for doc_id in to_delete:
            conn.execute(
                sa.text("DELETE FROM knowledgedocument WHERE id = :id"),
                {"id": doc_id},
            )
