"""ProjectTelemetry CRUD helpers.

Provides get/set access to the ``project_telemetry`` table, which stores
large code-written reserved blobs that were previously crowding
``project.design_doc``.  Writers dual-write to both design_doc (for backward
compat with readers not yet migrated) and this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import ProjectTelemetry
from app.time_utils import _utcnow

TELEMETRY_KEYS: frozenset[str] = frozenset(
    {
        # efficiency_baseline, lane_baselines, and sr_catch_rate_trends were moved
        # to dedicated tables (project_efficiency_snapshot, project_lane_baseline,
        # project_sr_analytics) as of migration 0656 and are no longer stored in
        # design_doc, so they are excluded from this set.
        "cochange_index",
        "cochange_applied_prs",
        "module_feedback",
        "retired_diagnoses_archive",
        "playbook_archive",
        "proposed_changes",
        # Per-direction first-attempt review feedback tallies.  Writers dual-write
        # to both design_doc and this table; readers fall back to design_doc.
        "direction_first_attempt_feedback",
    }
)


async def get_project_telemetry(
    session: AsyncSession,
    project_id: uuid.UUID,
    key: str,
    *,
    design_doc_fallback: dict | None = None,
) -> Any | None:
    """Return the stored value for (project_id, key), or None.

    If the telemetry row is absent and *design_doc_fallback* is provided,
    falls back to ``design_doc_fallback.get(key)`` so callers work correctly
    during the dual-write transition before all existing rows have been
    back-filled.
    """
    row = (
        await session.execute(
            select(ProjectTelemetry).where(
                ProjectTelemetry.project_id == project_id,
                ProjectTelemetry.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row.value
    if design_doc_fallback is not None:
        return design_doc_fallback.get(key)
    return None


async def set_project_telemetry(
    session: AsyncSession,
    project_id: uuid.UUID,
    key: str,
    value: Any,
) -> None:
    """Upsert *value* into ``project_telemetry`` for (project_id, key).

    Uses a PostgreSQL ``INSERT … ON CONFLICT DO UPDATE`` so concurrent writers
    converge to the last write rather than raising an integrity error.  The
    caller is responsible for committing the enclosing transaction.
    """
    now: datetime = _utcnow()
    stmt = (
        pg_insert(ProjectTelemetry)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            key=key,
            value=value,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=None,
            constraint="uq_project_telemetry_project_key",
            set_={"value": value, "updated_at": now},
        )
    )
    await session.execute(stmt)
