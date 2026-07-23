"""ProjectTelemetry CRUD helpers.

Provides get/set access to the ``project_telemetry`` table, which stores
large code-written reserved blobs that were previously crowding
``project.design_doc``.  Writers dual-write to both design_doc (for backward
compat with readers not yet migrated) and this table.

The ``project.design_doc`` JSONB column had accreted ~10 unrelated telemetry
blobs, bloating the row to 500 KB+ and forcing every background telemetry
writer to serialize through a single ``Project`` row lock
(``with_design_doc_locked`` rewrites the entire blob on each write). Migration
0659 introduced three dedicated tables; this module is the shared accessor the
follow-on writer subtasks consume so each caller does NOT re-implement the
upsert / retention logic:

* ``project_telemetry`` — latest-value KV store, keyed by (project_id, key)
* ``project_efficiency_snapshot`` — rolling time-series (retention-pruned)
* ``project_lane_baseline`` — per-release-version baselines (retention-pruned)

Every upsert here takes only the affected row lock (via ``ON CONFLICT DO
UPDATE`` on the natural key) — never the ``Project`` row lock. Callers are
responsible for committing the session.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Mapping

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ProjectEfficiencySnapshot,
    ProjectLaneBaseline,
    ProjectTelemetry,
)
from app.time_utils import _utcnow

# HEAD's set_project_telemetry uses `pg_insert` — alias for the same function
pg_insert = _pg_insert

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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
        # Completion-debt-ledger system description seeded by completion_debt.py.
        # Writers dual-write to both design_doc (for strategist visibility) and
        # this table; readers may fall back to design_doc.
        "systems/completion-debt-ledger",
    }
)

# Retention caps mirror the pre-existing in-design_doc caps so behavior is
# preserved after cutover:
#   * efficiency_snapshot.py::_SNAPSHOT_HISTORY_MAX = 7
#   * lane_baseline_snapshot.py::_MAX_BASELINES_STORED = 20
_EFFICIENCY_SNAPSHOT_HISTORY_MAX = 7
_LANE_BASELINE_VERSIONS_MAX = 20

__all__ = [
    "get_project_telemetry",
    "insert_efficiency_snapshot",
    "read_all_lane_baselines",
    "read_recent_efficiency_snapshots",
    "read_telemetry",
    "read_telemetry_many",
    "read_telemetry_or_doc",
    "read_version_lane_baseline",
    "set_project_telemetry",
    "upsert_lane_baseline",
    "upsert_telemetry",
]

# ---------------------------------------------------------------------------
# Latest-value KV store: project_telemetry
# ---------------------------------------------------------------------------


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


async def upsert_telemetry(
    session: AsyncSession,
    project_id: uuid.UUID,
    key: str,
    blob: Dict[str, Any],
) -> None:
    """Insert-or-overwrite the latest telemetry blob for (project_id, key).

    Acquires only the (project_id, key) row lock via ``ON CONFLICT
    (uq_project_telemetry) DO UPDATE`` — never the ``Project`` row lock.
    Caller commits.
    """
    stmt = _pg_insert(ProjectTelemetry).values(
        project_id=project_id,
        key=key,
        value=blob,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_project_telemetry_project_key",
        set_={
            "value": stmt.excluded.value,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def read_telemetry(
    session: AsyncSession,
    project_id: uuid.UUID,
    key: str,
) -> Dict[str, Any] | None:
    """Return the telemetry blob for (project_id, key), or ``None`` if absent."""
    result = await session.execute(
        select(ProjectTelemetry.value).where(
            ProjectTelemetry.project_id == project_id,
            ProjectTelemetry.key == key,
        )
    )
    row = result.first()
    return row[0] if row is not None else None


async def read_telemetry_many(
    session: AsyncSession,
    project_id: uuid.UUID,
    keys: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Batch-fetch telemetry blobs for several keys via ``WHERE key = ANY(...)``.

    Returns a ``{key: blob}`` dict containing only the keys that exist.
    """
    if not keys:
        return {}
    result = await session.execute(
        select(ProjectTelemetry.key, ProjectTelemetry.value).where(
            ProjectTelemetry.project_id == project_id,
            ProjectTelemetry.key.in_(list(keys)),
        )
    )
    return dict(result.all())


async def read_telemetry_or_doc(
    session: AsyncSession,
    project_id: uuid.UUID,
    design_doc: Mapping[str, Any] | None,
    key: str,
    default: Any = None,
) -> Any:
    """Telemetry-preferring read with a ``design_doc`` fallback.

    Prefers the ``project_telemetry`` row for ``key``; when no row exists (e.g.
    before backfill, or after a rollback that reverted the writer) falls back to
    ``design_doc[key]``, and finally ``default``. This is the single read seam
    that lets a value written only to the new table be observed by consumers
    while the legacy design_doc copy remains authoritative on rollback.
    """
    stored = await read_telemetry(session, project_id, key)
    if stored is not None:
        return stored
    if design_doc:
        return design_doc.get(key, default)
    return default


# ---------------------------------------------------------------------------
# Time-series: project_efficiency_snapshot
# ---------------------------------------------------------------------------


async def insert_efficiency_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    captured_at: datetime,
    metric_def_version: str,
    blob: Dict[str, Any],
) -> None:
    """Append (or overwrite by captured_at) an efficiency snapshot, then prune.

    Idempotent on (project_id, captured_at) via ``ON CONFLICT
    (uq_proj_efficiency_snap) DO UPDATE``. After the write, retains only the
    newest ``_EFFICIENCY_SNAPSHOT_HISTORY_MAX`` rows for the project,
    mirroring the WINDOW cap that ``efficiency_snapshot.py`` enforced inside
    ``design_doc``. Caller commits.
    """
    stmt = _pg_insert(ProjectEfficiencySnapshot).values(
        project_id=project_id,
        captured_at=captured_at,
        metric_def_version=metric_def_version,
        blob=blob,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_proj_efficiency_snap",
        set_={
            "metric_def_version": stmt.excluded.metric_def_version,
            "blob": stmt.excluded.blob,
        },
    )
    await session.execute(stmt)
    await _prune_efficiency_snapshots(session, project_id)


async def _prune_efficiency_snapshots(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """Delete all but the newest ``_EFFICIENCY_SNAPSHOT_HISTORY_MAX`` rows."""
    keep_ids = (
        select(ProjectEfficiencySnapshot.id)
        .where(ProjectEfficiencySnapshot.project_id == project_id)
        .order_by(ProjectEfficiencySnapshot.captured_at.desc())
        .limit(_EFFICIENCY_SNAPSHOT_HISTORY_MAX)
        .scalar_subquery()
    )
    await session.execute(
        delete(ProjectEfficiencySnapshot).where(
            ProjectEfficiencySnapshot.project_id == project_id,
            ProjectEfficiencySnapshot.id.not_in(keep_ids),
        )
    )


async def read_recent_efficiency_snapshots(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int = _EFFICIENCY_SNAPSHOT_HISTORY_MAX,
) -> List[ProjectEfficiencySnapshot]:
    """Return the newest ``limit`` snapshots for the project, captured_at DESC."""
    result = await session.execute(
        select(ProjectEfficiencySnapshot)
        .where(ProjectEfficiencySnapshot.project_id == project_id)
        .order_by(ProjectEfficiencySnapshot.captured_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Per-version keyed: project_lane_baseline
# ---------------------------------------------------------------------------


async def upsert_lane_baseline(
    session: AsyncSession,
    project_id: uuid.UUID,
    version_number: int,
    captured_at: datetime,
    baseline_def_version: str,
    blob: Dict[str, Any],
) -> None:
    """Insert-or-overwrite the lane baseline for (project_id, version_number).

    Idempotent via ``ON CONFLICT (uq_proj_lane_baseline) DO UPDATE``. After the
    write, retains only the most recent ``_LANE_BASELINE_VERSIONS_MAX``
    versions per project, mirroring the ``_MAX_BASELINES_STORED`` cap that
    ``lane_baseline_snapshot.py`` enforced inside ``design_doc``. Caller
    commits.
    """
    stmt = _pg_insert(ProjectLaneBaseline).values(
        project_id=project_id,
        version_number=version_number,
        captured_at=captured_at,
        baseline_def_version=baseline_def_version,
        blob=blob,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_proj_lane_baseline",
        set_={
            "captured_at": stmt.excluded.captured_at,
            "baseline_def_version": stmt.excluded.baseline_def_version,
            "blob": stmt.excluded.blob,
        },
    )
    await session.execute(stmt)
    await _prune_lane_baselines(session, project_id)


async def _prune_lane_baselines(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """Delete all but the most recent ``_LANE_BASELINE_VERSIONS_MAX`` versions."""
    keep_ids = (
        select(ProjectLaneBaseline.id)
        .where(ProjectLaneBaseline.project_id == project_id)
        .order_by(ProjectLaneBaseline.version_number.desc())
        .limit(_LANE_BASELINE_VERSIONS_MAX)
        .scalar_subquery()
    )
    await session.execute(
        delete(ProjectLaneBaseline).where(
            ProjectLaneBaseline.project_id == project_id,
            ProjectLaneBaseline.id.not_in(keep_ids),
        )
    )


async def read_version_lane_baseline(
    session: AsyncSession,
    project_id: uuid.UUID,
    version_number: int,
) -> Dict[str, Any] | None:
    """Return the lane-baseline blob for (project_id, version_number), or None."""
    result = await session.execute(
        select(ProjectLaneBaseline.blob).where(
            ProjectLaneBaseline.project_id == project_id,
            ProjectLaneBaseline.version_number == version_number,
        )
    )
    row = result.first()
    return row[0] if row is not None else None


async def read_all_lane_baselines(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> List[ProjectLaneBaseline]:
    """Return every stored lane baseline row for the project, version DESC.

    The summary reader (``admin_analytics.get_lane_baselines``) enumerates all
    per-version baselines; this accessor lets it prefer the dedicated table over
    ``design_doc['lane_baselines']`` without re-implementing the query.
    """
    result = await session.execute(
        select(ProjectLaneBaseline)
        .where(ProjectLaneBaseline.project_id == project_id)
        .order_by(ProjectLaneBaseline.captured_at.desc())
    )
    return list(result.scalars().all())
