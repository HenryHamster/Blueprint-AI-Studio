"""Repair Workday message linkage and direction telemetry.

Revision ID: 0682
Revises: 0681
Create Date: 2026-07-22

The previously released revision 0657 removed
``projectmessage.resident_session_id`` before 0671 restored it empty. The
amended chain now preserves the column, while this tail repair recovers values
for already-upgraded databases only where same-project resident task context is
durable evidence. Rows that cannot be attributed directly remain NULL.

The previously released revision 0669 also serialized JSONB incorrectly. The
amended chain now uses real JSON serialization; this tail repeats the safe,
insert-only backfill for already-upgraded databases.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Mapping

import sqlalchemy as sa
from alembic import op


revision = "0682"
down_revision = "0681"
branch_labels = None
depends_on = None

_TELEMETRY_KEY = "direction_first_attempt_feedback"


def _decoded_object(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_payload(value: Any) -> str:
    """Return standards-compliant JSON for a decoded JSON/JSONB value."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    decoded = _decoded_object(value)
    return decoded if isinstance(decoded, Mapping) else {}


def _resident_reply_session(
    row: Mapping[str, Any],
    task_sessions: Mapping[str, tuple[str, str]],
) -> str | None:
    """Return the proven resident session for a strategic reply row."""

    if str(row.get("from_type") or "") != "strategic":
        return None
    task_id = row.get("from_task_id")
    task_session = task_sessions.get(str(task_id)) if task_id is not None else None
    if task_session and task_session[0] == str(row.get("project_id")):
        return task_session[1]
    return None


def _recover_session_updates(
    task_rows: Iterable[Mapping[str, Any]],
    message_rows: Iterable[Mapping[str, Any]],
) -> dict[Any, str]:
    """Recover only strategic rows directly anchored by resident tasks."""

    task_sessions: dict[str, tuple[str, str]] = {}
    for row in task_rows:
        context = _as_mapping(row.get("context"))
        session_id = str(context.get("strategic_session_id") or "").strip()
        if context.get("_resident_session") is True and session_id:
            task_sessions[str(row["id"])] = (
                str(row.get("project_id")),
                session_id,
            )

    updates: dict[Any, str] = {}
    for row in message_rows:
        if str(row.get("resident_session_id") or "").strip():
            continue
        session_id = _resident_reply_session(row, task_sessions)
        if session_id:
            updates[row["id"]] = session_id
    return updates


def _repair_telemetry(conn: sa.Connection) -> None:
    project_query = "SELECT id, design_doc FROM project ORDER BY id"
    rows = conn.execute(sa.text(project_query)).mappings().all()

    if conn.dialect.name == "postgresql":
        upsert = sa.text(
            "INSERT INTO project_telemetry (id, project_id, key, value, updated_at) "
            "VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), :key, "
            "CAST(:value AS jsonb), CURRENT_TIMESTAMP) "
            "ON CONFLICT ON CONSTRAINT uq_project_telemetry_project_key "
            "DO NOTHING"
        )
    else:
        upsert = sa.text(
            "INSERT INTO project_telemetry (id, project_id, key, value, updated_at) "
            "VALUES (:id, :project_id, :key, :value, CURRENT_TIMESTAMP) "
            "ON CONFLICT(project_id, key) DO NOTHING"
        )

    for row in rows:
        design_doc = _as_mapping(row["design_doc"])
        if _TELEMETRY_KEY not in design_doc or design_doc[_TELEMETRY_KEY] is None:
            continue
        telemetry_id = uuid.uuid4()
        conn.execute(
            upsert,
            {
                "id": (
                    telemetry_id
                    if conn.dialect.name == "postgresql"
                    else str(telemetry_id)
                ),
                "project_id": row["id"],
                "key": _TELEMETRY_KEY,
                "value": _json_payload(design_doc[_TELEMETRY_KEY]),
            },
        )


def _repair_message_sessions(conn: sa.Connection) -> None:
    task_rows = (
        conn.execute(sa.text("SELECT id, project_id, context FROM task ORDER BY id"))
        .mappings()
        .all()
    )
    message_rows = (
        conn.execute(
            sa.text(
                "SELECT id, project_id, from_type, from_task_id, "
                "resident_session_id FROM projectmessage ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    for message_id, session_id in _recover_session_updates(
        task_rows, message_rows
    ).items():
        conn.execute(
            sa.text(
                "UPDATE projectmessage SET resident_session_id = :session_id "
                "WHERE id = :message_id AND resident_session_id IS NULL"
            ),
            {"message_id": message_id, "session_id": session_id},
        )


def upgrade() -> None:
    conn = op.get_bind()
    _repair_telemetry(conn)
    _repair_message_sessions(conn)


def downgrade() -> None:
    # Both repairs restore valid durable data. Erasing it on downgrade would
    # recreate the incident and cannot reconstruct the pre-repair ambiguity.
    pass
