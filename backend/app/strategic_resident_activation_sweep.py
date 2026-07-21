"""One-time activation repair for authoritative Strategic Residents."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import StrategicResident
from app.strategic_resident_wakeups import sync_resident_background_policy
from app.time_utils import _utcnow


_DEFAULT_EMPTY_SCAN_COOLDOWN = timedelta(hours=6)
_empty_scan_not_before: datetime | None = None


def _has_valid_activation_sequence(scheduling_state: object) -> bool:
    if not isinstance(scheduling_state, dict):
        return False
    raw_sequence = scheduling_state.get("background_activation_sequence")
    return (
        isinstance(raw_sequence, int)
        and not isinstance(raw_sequence, bool)
        and raw_sequence > 0
    )


async def _candidate_project_ids(
    scan: AsyncSession,
    *,
    bounded_batch: int,
) -> list[uuid.UUID]:
    authoritative_active = (
        StrategicResident.engine_owner == "strategic_resident",
        StrategicResident.background_policy == "active",
    )
    dialect_name = resolve_dialect_name(scan)
    if dialect_name == "postgresql":
        marker = StrategicResident.scheduling_state[
            "background_activation_sequence"
        ]
        marker_type = func.jsonb_typeof(marker)
        marker_text = marker.as_string()
        valid_marker = and_(
            marker_type == "number",
            marker_text.op("~")(r"^[1-9][0-9]*$"),
        )
        return list(
            (
                await scan.execute(
                    select(StrategicResident.project_id)
                    .where(
                        *authoritative_active,
                        or_(marker_type.is_(None), not_(valid_marker)),
                    )
                    .order_by(StrategicResident.project_id.asc())
                    .limit(bounded_batch)
                )
            ).scalars()
        )

    if dialect_name == "sqlite":
        marker_path = "$.background_activation_sequence"
        marker_type = func.json_type(
            StrategicResident.scheduling_state,
            marker_path,
        )
        marker_value = func.json_extract(
            StrategicResident.scheduling_state,
            marker_path,
        )
        valid_marker = and_(marker_type == "integer", marker_value > 0)
        return list(
            (
                await scan.execute(
                    select(StrategicResident.project_id)
                    .where(
                        *authoritative_active,
                        or_(marker_type.is_(None), not_(valid_marker)),
                    )
                    .order_by(StrategicResident.project_id.asc())
                    .limit(bounded_batch)
                )
            ).scalars()
        )

    # Unknown dialects get a portable fallback. Page through all authoritative
    # active rows and validate in Python; limiting before validation would let
    # an early run of valid rows permanently hide later repair candidates.
    project_ids: list[uuid.UUID] = []
    after_project_id: uuid.UUID | None = None
    page_size = max(32, bounded_batch)
    while len(project_ids) < bounded_batch:
        statement = select(
            StrategicResident.project_id,
            StrategicResident.scheduling_state,
        ).where(*authoritative_active)
        if after_project_id is not None:
            statement = statement.where(
                StrategicResident.project_id > after_project_id
            )
        rows = (
            await scan.execute(
                statement.order_by(StrategicResident.project_id.asc()).limit(page_size)
            )
        ).all()
        if not rows:
            break
        for project_id, scheduling_state in rows:
            if not _has_valid_activation_sequence(scheduling_state):
                project_ids.append(project_id)
                if len(project_ids) == bounded_batch:
                    break
        after_project_id = rows[-1][0]
        if len(rows) < page_size:
            break
    return project_ids


async def sweep_missing_background_activation_wakeups(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    batch_size: int = 32,
    empty_scan_cooldown: timedelta = _DEFAULT_EMPTY_SCAN_COOLDOWN,
) -> int:
    """Repair active authoritative Residents created before activation bootstraps.

    PostgreSQL and SQLite filter invalid markers in their native JSON dialect;
    unknown dialects page through active rows and validate markers in Python.
    Every invalid or missing marker remains eligible until its row is repaired,
    while the bounded batch prevents deploy catch-up from monopolizing a cycle.

    Discovery is deliberately separate from mutation. Each candidate is locked
    and revalidated before the policy helper runs, so concurrent reapers can
    observe the same row but only the first advances its activation sequence.
    A concurrent pause or ownership rollback is never reversed by this repair.
    """

    global _empty_scan_not_before

    bounded_batch = max(1, min(batch_size, 200))
    observed_at = now or _utcnow()
    cooldown = max(empty_scan_cooldown, timedelta(0))
    if (
        cooldown
        and _empty_scan_not_before is not None
        and observed_at < _empty_scan_not_before
    ):
        return 0

    async with session_factory() as scan:
        project_ids = await _candidate_project_ids(
            scan,
            bounded_batch=bounded_batch,
        )
    if not project_ids:
        _empty_scan_not_before = observed_at + cooldown if cooldown else None
        return 0
    # Keep draining deploy catch-up batches on each reaper cycle. Cool down only
    # after a scan proves that no eligible rows remain.
    _empty_scan_not_before = None

    queued = 0
    for project_id in project_ids:
        async with session_factory() as session, session.begin():
            resident = (
                await session.execute(
                    select(StrategicResident)
                    .where(StrategicResident.project_id == project_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                resident is None
                or resident.engine_owner != "strategic_resident"
                or resident.background_policy != "active"
            ):
                continue
            if _has_valid_activation_sequence(resident.scheduling_state):
                continue
            wake = await sync_resident_background_policy(
                session,
                project_id=project_id,
                paused=False,
                now=observed_at,
            )
            if wake is not None and wake.task_id is not None:
                queued += 1
    return queued


__all__ = ["sweep_missing_background_activation_wakeups"]
