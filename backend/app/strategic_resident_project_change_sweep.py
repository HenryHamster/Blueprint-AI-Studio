"""Durable project-change producers for authoritative Resident wakeups."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import (
    QualificationVerdict,
    StrategicCycle,
    StrategicResident,
    StrategicResidentOrdinaryTaskTerminalEvent,
    StrategicResidentSweepState,
    StrategicResidentTaskTerminalEvent,
)
from app.strategic_cycle import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED_PHASE1,
    OUTCOME_FAILED_PHASE2,
    OUTCOME_FAILED_PHASE2_SPAWN,
    OUTCOME_STALLED_PHASE1,
    OUTCOME_STALLED_PHASE2,
    OUTCOME_THINKER_ARTIFACTS_MISSING,
)
from app.strategic_resident_wakeups import signal_project_change
from app.strategic_resident_turns import _merge_project_change_cursor


_TERMINAL_STRATEGIC_CYCLE_OUTCOMES = (
        OUTCOME_COMPLETED,
        OUTCOME_FAILED_PHASE1,
        OUTCOME_FAILED_PHASE2,
        OUTCOME_FAILED_PHASE2_SPAWN,
        OUTCOME_STALLED_PHASE1,
        OUTCOME_STALLED_PHASE2,
        OUTCOME_THINKER_ARTIFACTS_MISSING,
)
_TERMINAL_CYCLE_PREDICATE = text(
    "strategiccycle.outcome IN ("
    + ",".join(f"'{outcome}'" for outcome in _TERMINAL_STRATEGIC_CYCLE_OUTCOMES)
    + ")"
)
_SWEEP_NAME = "durable_project_change"
_MAX_WAKE_BATCH = 32


def _event_cursor(*, observed_at: datetime, row_id: object, outcome: str) -> str:
    """Return a comparable, bounded high-water value for one durable event."""

    normalized = observed_at
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    elapsed = normalized - epoch
    epoch_micros = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    return f"v2:{epoch_micros:020d}:{row_id}:{outcome}"


def _latest_scalar(
    column: Any,
    *,
    filters: tuple[Any, ...],
    order_by: tuple[Any, ...],
    label: str,
) -> Any:
    return (
        select(column)
        .where(*filters)
        .order_by(*order_by)
        .limit(1)
        .correlate(StrategicResident)
        .scalar_subquery()
        .label(label)
    )


def _resident_event_statement() -> Any:
    cycle_filters = (
        StrategicCycle.project_id == StrategicResident.project_id,
        # Keep the terminal set literal so PostgreSQL can prove the query
        # implies migration 0635's partial-index predicate even after asyncpg
        # switches a prepared statement to a generic plan.
        _TERMINAL_CYCLE_PREDICATE,
    )
    cycle_order = (StrategicCycle.updated_at.desc(), StrategicCycle.id.desc())
    verdict_filters = (
        QualificationVerdict.project_id == StrategicResident.project_id,
    )
    verdict_order = (
        QualificationVerdict.created_at.desc(),
        QualificationVerdict.id.desc(),
    )
    columns = [
        StrategicResident.project_id,
        StrategicResident.handled_project_change_cursor,
        StrategicResident.recovery_state,
        _latest_scalar(
            StrategicCycle.updated_at,
            filters=cycle_filters,
            order_by=cycle_order,
            label="cycle_observed_at",
        ),
        _latest_scalar(
            StrategicCycle.id,
            filters=cycle_filters,
            order_by=cycle_order,
            label="cycle_row_id",
        ),
        _latest_scalar(
            StrategicCycle.outcome,
            filters=cycle_filters,
            order_by=cycle_order,
            label="cycle_outcome",
        ),
        _latest_scalar(
            QualificationVerdict.created_at,
            filters=verdict_filters,
            order_by=verdict_order,
            label="verdict_observed_at",
        ),
        _latest_scalar(
            QualificationVerdict.id,
            filters=verdict_filters,
            order_by=verdict_order,
            label="verdict_row_id",
        ),
        _latest_scalar(
            QualificationVerdict.terminal_status,
            filters=verdict_filters,
            order_by=verdict_order,
            label="verdict_outcome",
        ),
    ]
    terminal_filters = (
        StrategicResidentTaskTerminalEvent.project_id
        == StrategicResident.project_id,
    )
    terminal_order = (StrategicResidentTaskTerminalEvent.sequence.desc(),)
    columns.extend(
        (
            _latest_scalar(
                StrategicResidentTaskTerminalEvent.sequence,
                filters=terminal_filters,
                order_by=terminal_order,
                label="managed_task_terminal_sequence",
            ),
            _latest_scalar(
                StrategicResidentTaskTerminalEvent.task_id,
                filters=terminal_filters,
                order_by=terminal_order,
                label="managed_task_terminal_task_id",
            ),
            _latest_scalar(
                StrategicResidentTaskTerminalEvent.event_kind,
                filters=terminal_filters,
                order_by=terminal_order,
                label="managed_task_terminal_status",
            ),
        )
    )
    ordinary_filters = (
        StrategicResidentOrdinaryTaskTerminalEvent.project_id
        == StrategicResident.project_id,
    )
    ordinary_order = (
        StrategicResidentOrdinaryTaskTerminalEvent.sequence.desc(),
    )
    columns.extend(
        (
            _latest_scalar(
                StrategicResidentOrdinaryTaskTerminalEvent.sequence,
                filters=ordinary_filters,
                order_by=ordinary_order,
                label="ordinary_task_terminal_sequence",
            ),
            _latest_scalar(
                StrategicResidentOrdinaryTaskTerminalEvent.task_id,
                filters=ordinary_filters,
                order_by=ordinary_order,
                label="ordinary_task_terminal_task_id",
            ),
            _latest_scalar(
                StrategicResidentOrdinaryTaskTerminalEvent.event_kind,
                filters=ordinary_filters,
                order_by=ordinary_order,
                label="ordinary_task_terminal_status",
            ),
        )
    )
    return select(*columns).where(
        StrategicResident.engine_owner == "strategic_resident",
        StrategicResident.background_policy == "active",
    )


async def _bounded_resident_rows(
    scan: AsyncSession,
    *,
    batch_size: int,
) -> tuple[list[Any], int]:
    """Round-robin a bounded Resident page without writing unchanged rows."""

    state = (
        await scan.execute(
            select(StrategicResidentSweepState)
            .where(StrategicResidentSweepState.sweep_name == _SWEEP_NAME)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if state is None:
        # Metadata-created test databases do not run the migration seed. The
        # production migration creates this row, while this idempotent fallback
        # keeps local/new databases restart-durable too.
        state = StrategicResidentSweepState(sweep_name=_SWEEP_NAME)
        scan.add(state)
        await scan.flush()
    after = state.after_project_id
    statement = _resident_event_statement()
    if after is not None:
        statement = statement.where(StrategicResident.project_id > after)
    rows = list(
        (
            await scan.execute(
                statement.order_by(StrategicResident.project_id.asc()).limit(
                    batch_size
                )
            )
        ).all()
    )
    if len(rows) < batch_size and after is not None:
        wrapped = (
            await scan.execute(
                _resident_event_statement()
                .where(StrategicResident.project_id <= after)
                .order_by(StrategicResident.project_id.asc())
                .limit(batch_size - len(rows))
            )
        ).all()
        rows.extend(wrapped)
    return rows, int(state.version)


async def _persist_scan_position(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    after_project_id: uuid.UUID,
    expected_version: int,
    now: datetime,
) -> None:
    """Advance the durable scan cursor with a non-regressing CAS."""

    async with session_factory() as session, session.begin():
        # A concurrent/replayed sweep may have advanced first. Losing this CAS
        # only causes harmless reinspection next tick; it can never move the
        # shared high-water backwards or skip an uninspected project.
        await session.execute(
            update(StrategicResidentSweepState)
            .where(
                StrategicResidentSweepState.sweep_name == _SWEEP_NAME,
                StrategicResidentSweepState.version == expected_version,
            )
            .values(
                after_project_id=after_project_id,
                version=StrategicResidentSweepState.version + 1,
                updated_at=now,
            )
        )


async def sweep_durable_project_change_wakeups(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    batch_size: int = 32,
    scan_batch_size: int = 200,
) -> int:
    """Wake Residents for new durable project outcomes.

    All sources are durable and project-scoped. Strategic cycles are included
    only after reaching a known terminal outcome, so phase progress cannot cause
    feedback wakeups. Qualification verdicts are immutable finalize records.
    The latest row from each source becomes an opaque bounded cursor. Once that
    cursor is represented by Resident's handled cursor, repeated sweeps reject
    it before a Task exists, preserving the zero-model-call cadence contract.

    The scan is limited to authoritative, active Residents. Paused projects keep
    their durable rows and are discovered after activation instead of losing the
    event. Managed Task terminal transitions come from an append-only,
    sequence-ordered ledger; its write-time lineage gate structurally excludes
    Resident turn Tasks and unrelated pipeline work. Ordinary Task outcomes
    admit only operator-authored roots, excluding pipeline children and every
    Resident-authored Task. At most 32 projects are signalled per sweep even
    when every source is busy.
    """

    # This is a model-call capacity boundary, not only a caller default. Keep
    # misconfigured/manual callers from turning a cheap 200-row scan into a
    # 200-turn burst.
    bounded_batch = max(1, min(batch_size, _MAX_WAKE_BATCH))
    # Read breadth and wake/model-call capacity are deliberately independent.
    # The leader cheaply inspects a full bounded page every tick while still
    # creating at most 32 turns, so 1k active projects complete one
    # fair rotation in at most five normal reaper ticks.
    bounded_scan = max(bounded_batch, min(200, max(1, scan_batch_size)))
    async with session_factory() as scan, scan.begin():
        rows, scan_version = await _bounded_resident_rows(
            scan, batch_size=bounded_scan
        )

    queued = 0
    last_inspected_project_id: uuid.UUID | None = None
    for row in rows:
        # Persist only across rows actually inspected. If the queue budget is
        # reached, the next tick resumes here instead of skipping the scan tail.
        last_inspected_project_id = row.project_id
        recovery = row.recovery_state if isinstance(row.recovery_state, dict) else {}
        if recovery.get("unknown_operation_receipt_ids"):
            continue
        handled = (
            row.handled_project_change_cursor
            if isinstance(row.handled_project_change_cursor, dict)
            else {}
        )
        candidate_cursor: dict[str, str] = {}
        if row.cycle_observed_at is not None:
            cycle_value = _event_cursor(
                observed_at=row.cycle_observed_at,
                row_id=row.cycle_row_id,
                outcome=str(row.cycle_outcome),
            )
            candidate_cursor["strategic_cycle_terminal"] = cycle_value
        if row.verdict_observed_at is not None:
            verdict_value = _event_cursor(
                observed_at=row.verdict_observed_at,
                row_id=row.verdict_row_id,
                outcome=str(row.verdict_outcome),
            )
            candidate_cursor["qualification_verdict"] = verdict_value
        if row.managed_task_terminal_sequence is not None:
            candidate_cursor["managed_task_terminal"] = (
                f"v2:{int(row.managed_task_terminal_sequence):020d}:"
                f"{row.managed_task_terminal_task_id}:"
                f"{row.managed_task_terminal_status}"
            )
        if row.ordinary_task_terminal_sequence is not None:
            candidate_cursor["ordinary_task_terminal"] = (
                f"v2:{int(row.ordinary_task_terminal_sequence):020d}:"
                f"{row.ordinary_task_terminal_task_id}:"
                f"{row.ordinary_task_terminal_status}"
            )
        represented = _merge_project_change_cursor({}, handled)
        advanced = _merge_project_change_cursor(represented, candidate_cursor)
        cursor = {
            key: value
            for key, value in advanced.items()
            if represented.get(key) != value
        }
        if not cursor:
            continue
        wake_task_id = await signal_project_change(
            session_factory,
            project_id=row.project_id,
            project_change_cursor=cursor,
            now=now,
        )
        if wake_task_id is not None:
            queued += 1
            if queued >= bounded_batch:
                break
    if last_inspected_project_id is not None:
        await _persist_scan_position(
            session_factory,
            after_project_id=last_inspected_project_id,
            expected_version=scan_version,
            now=now or datetime.now(timezone.utc).replace(tzinfo=None),
        )
    return queued


__all__ = ["sweep_durable_project_change_wakeups"]
