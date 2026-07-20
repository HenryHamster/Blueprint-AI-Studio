"""Locked task inventory for Strategic Resident ownership transitions."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, StrategicCycle, StrategicResident, Task, TaskStatus
from app.strategic_resident_legacy_projection import (
    LEGACY_CONTINUATION_KEY,
    LEGACY_SESSION_KEY,
)
from app.strategic_resident_legacy_authority import (
    LegacyEngineAuthorityExpired as _LegacyEngineAuthorityExpired,
    ResidentOwnershipTransitionError as _ResidentOwnershipTransitionError,
    commit_legacy_authorized as _commit_legacy_authorized,
    lock_and_validate_legacy_authority as _lock_and_validate_legacy_authority,
)
from app.strategic_resident_turns import STRATEGIC_RESIDENT_TURN_TASK_TYPE

LegacyEngineAuthorityExpired = _LegacyEngineAuthorityExpired
ResidentOwnershipTransitionError = _ResidentOwnershipTransitionError
commit_legacy_authorized = _commit_legacy_authorized
lock_and_validate_legacy_authority = _lock_and_validate_legacy_authority


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
LEGACY_RESIDENT_TASK_CONTEXT_KEY = "_resident_session"
LEGACY_RESIDENT_SESSION_ID_CONTEXT_KEY = "strategic_session_id"
ACTIVE_LEGACY_CYCLE_OUTCOMES = (
    "in_progress",
    "phase1_running",
    "phase1_done",
    "phase2_running",
)
LEGACY_CYCLE_TASK_TYPES = ("design_doc_update", "strategic_review")
_RECONCILABLE_CYCLE_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class ResidentCheckpointBoundaryError(ResidentOwnershipTransitionError):
    """The Resident has no valid current checkpoint boundary."""


class ResidentOwnershipStateError(ResidentOwnershipTransitionError):
    """The Resident control plane is inconsistent with the requested transition."""


class UnreconciledResidentOperations(ResidentOwnershipTransitionError):
    """Rollback is fenced until uncertain operation effects are reconciled."""

    def __init__(self, receipt_ids: list[uuid.UUID]):
        self.receipt_ids = tuple(receipt_ids)
        super().__init__(
            "rollback blocked by unreconciled Resident operations: "
            + ", ".join(str(item) for item in self.receipt_ids)
        )


class LegacyResidentDrainRequired(ResidentOwnershipTransitionError):
    """Cutover is blocked until the legacy Resident is canonically drained."""


def _payload(task: Task) -> dict:
    return task.first_payload if isinstance(task.first_payload, dict) else {}


def _active_cycle_phase(
    cycle: StrategicCycle,
) -> tuple[str, uuid.UUID | None, str]:
    if cycle.outcome in {"in_progress", "phase1_running"}:
        return "phase1", cycle.phase1_task_id, "design_doc_update"
    if cycle.outcome == "phase2_running":
        return "phase2", cycle.phase2_task_id, "strategic_review"
    raise LegacyResidentDrainRequired(
        f"legacy StrategicCycle {cycle.id} remains active in {cycle.outcome}"
    )


def _reconcile_failed_active_cycle(
    cycle: StrategicCycle,
    task: Task,
    *,
    phase: str,
    reconciled_at: datetime,
) -> None:
    status = str(getattr(task.status, "value", task.status))
    terminal_at = task.completed_at or task.updated_at or reconciled_at
    previous_outcome = cycle.outcome
    if phase == "phase1":
        cycle.outcome = "failed_phase1"
        cycle.phase1_completed_at = cycle.phase1_completed_at or terminal_at
    else:
        cycle.outcome = "failed_phase2"
        cycle.phase2_completed_at = cycle.phase2_completed_at or terminal_at
    reason = (
        f"cutover_reconciliation: linked {phase} Task {task.id} reached "
        f"{status} while StrategicCycle remained {previous_outcome}"
    )
    detail = (task.latest_status_message or "").strip()
    cycle.failure_reason = (f"{reason}: {detail}" if detail else reason)[:2000]
    cycle.updated_at = reconciled_at


def _cycle_lineage_matches(cycle: StrategicCycle, task: Task) -> bool:
    canonical = task.strategic_cycle_id
    context = task.context if isinstance(task.context, dict) else {}
    if canonical is not None:
        if str(canonical) != cycle.id:
            return False
        contextual = context.get("strategic_cycle_id")
        return contextual is None or str(contextual) == cycle.id
    return str(context.get("strategic_cycle_id") or "") == cycle.id


async def lock_project_resident_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    reject_active_legacy_cycles: bool = False,
    reconciliation_at: datetime | None = None,
) -> tuple[Project, StrategicResident, list[Task]]:
    """Lock the transition root and complete live Resident task inventory."""
    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None:
        raise ResidentOwnershipStateError("project not found")
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if resident is None:
        raise ResidentOwnershipStateError("Strategic Resident not found")

    active_cycles = (
        (
            await session.execute(
                select(StrategicCycle)
                .where(
                    StrategicCycle.project_id == project_id,
                    StrategicCycle.outcome.in_(ACTIVE_LEGACY_CYCLE_OUTCOMES),
                )
                .order_by(StrategicCycle.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    vision = project.vision if isinstance(project.vision, dict) else {}
    legacy_blob = vision.get(LEGACY_SESSION_KEY)
    legacy_task_id: uuid.UUID | None = None
    if isinstance(legacy_blob, Mapping) and legacy_blob.get("task_id"):
        try:
            legacy_task_id = uuid.UUID(str(legacy_blob["task_id"]))
        except (TypeError, ValueError):
            raise ResidentOwnershipStateError(
                "legacy Resident session references a malformed task id"
            ) from None
    pointer_ids = {
        task_id
        for task_id in (
            resident.active_turn_task_id,
            resident.interactive_successor_task_id,
            resident.background_successor_task_id,
            legacy_task_id,
        )
        if task_id is not None
    }
    cycle_task_ids: set[uuid.UUID] = set()
    if active_cycles and reject_active_legacy_cycles:
        for cycle in active_cycles:
            _phase, task_id, _task_type = _active_cycle_phase(cycle)
            if task_id is not None:
                cycle_task_ids.add(task_id)
    locked_task_ids = sorted(pointer_ids | cycle_task_ids, key=str)
    tasks: list[Task] = []
    if locked_task_ids:
        tasks.extend(
            (
                await session.execute(
                    select(Task)
                    .where(Task.id.in_(locked_task_ids))
                    .order_by(Task.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    query = (
        select(Task)
        .where(
            Task.project_id == project_id,
            or_(
                Task.payloads[0]["type"].as_string()
                == STRATEGIC_RESIDENT_TURN_TASK_TYPE,
                Task.context[LEGACY_RESIDENT_TASK_CONTEXT_KEY]
                .as_boolean()
                .is_(True),
                and_(
                    Task.payloads[0]["type"].as_string().in_(
                        LEGACY_CYCLE_TASK_TYPES
                    ),
                    Task.context["strategic_cycle_id"].as_string().is_not(None),
                ),
            ),
            Task.status.not_in(list(TERMINAL_TASK_STATUSES)),
        )
        .order_by(Task.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_task_ids:
        query = query.where(Task.id.not_in(locked_task_ids))
    tasks.extend((await session.execute(query)).scalars().all())
    by_id = {task.id: task for task in tasks}
    for attr in (
        "active_turn_task_id",
        "interactive_successor_task_id",
        "background_successor_task_id",
    ):
        task_id = getattr(resident, attr)
        if task_id is None:
            continue
        task = by_id.get(task_id)
        if task is None:
            raise ResidentOwnershipStateError(f"Resident {attr} references a missing Task")
        if task.project_id != project_id:
            raise ResidentOwnershipStateError(
                f"Resident {attr} references a cross-project Task"
            )
        if _payload(task).get("type") != STRATEGIC_RESIDENT_TURN_TASK_TYPE:
            raise ResidentOwnershipStateError(
                f"Resident {attr} references a non-Resident Task"
            )
    if legacy_task_id is not None:
        task = by_id.get(legacy_task_id)
        if task is None:
            raise ResidentOwnershipStateError(
                "legacy Resident session references a missing Task"
            )
        if task.project_id != project_id:
            raise ResidentOwnershipStateError(
                "legacy Resident session references a cross-project Task"
            )
        context = task.context if isinstance(task.context, dict) else {}
        if context.get(LEGACY_RESIDENT_TASK_CONTEXT_KEY) is not True:
            raise ResidentOwnershipStateError(
                "legacy Resident session references a non-session Task"
            )
        expected = str(legacy_blob.get("session_id") or "")
        if expected and str(context.get(LEGACY_RESIDENT_SESSION_ID_CONTEXT_KEY) or "") != expected:
            raise ResidentOwnershipStateError(
                "legacy Resident session Task lineage is inconsistent"
            )
    if active_cycles and reject_active_legacy_cycles:
        if resident.engine_owner != "legacy":
            raise ResidentOwnershipStateError(
                "authoritative Resident still has active legacy StrategicCycle work"
            )
        blockers: list[str] = []
        reconciliations: list[tuple[StrategicCycle, Task, str]] = []
        reconciled_at = reconciliation_at or project.updated_at
        for cycle in active_cycles:
            phase, task_id, expected_task_type = _active_cycle_phase(cycle)
            task = by_id.get(task_id) if task_id is not None else None
            reason: str | None = None
            if task_id is None:
                reason = f"{cycle.id}: missing {phase} Task pointer"
            elif task is None:
                reason = f"{cycle.id}: linked Task {task_id} is missing"
            elif task.project_id != project_id:
                reason = f"{cycle.id}: linked Task {task_id} is cross-project"
            elif _payload(task).get("type") != expected_task_type:
                reason = f"{cycle.id}: linked Task {task_id} has wrong phase type"
            elif not _cycle_lineage_matches(cycle, task):
                reason = f"{cycle.id}: linked Task {task_id} has stale cycle lineage"
            elif task.status not in _RECONCILABLE_CYCLE_TASK_STATUSES:
                reason = (
                    f"{cycle.id}: linked Task {task_id} remains "
                    f"{getattr(task.status, 'value', task.status)}"
                )
            elif task.post_commit_pending:
                reason = (
                    f"{cycle.id}: linked Task {task_id} has post-commit work pending"
                )
            if reason is not None:
                blockers.append(reason)
                continue
            reconciliations.append((cycle, task, phase))
        if blockers:
            raise LegacyResidentDrainRequired(
                "legacy StrategicCycle work is still active: " + "; ".join(blockers)
            )
        for cycle, task, phase in reconciliations:
            _reconcile_failed_active_cycle(
                cycle,
                task,
                phase=phase,
                reconciled_at=reconciled_at,
            )
            session.add(cycle)

    return project, resident, tasks


def assert_legacy_resident_drained(project: Project, tasks: list[Task]) -> None:
    """Require all canonical legacy session and StrategicCycle work to be drained."""
    vision = project.vision if isinstance(project.vision, dict) else {}
    if vision.get(LEGACY_CONTINUATION_KEY) is not None:
        raise LegacyResidentDrainRequired(
            "legacy Resident continuation lease is still present"
        )
    blob = vision.get(LEGACY_SESSION_KEY)
    if isinstance(blob, Mapping) and (
        blob.get("status") != "ended" or blob.get("stop_requested") is not True
    ):
        raise LegacyResidentDrainRequired(
            "legacy Resident session is not ended and stop-requested"
        )
    for task in tasks:
        context = task.context if isinstance(task.context, dict) else {}
        live = task.status not in TERMINAL_TASK_STATUSES
        if context.get(LEGACY_RESIDENT_TASK_CONTEXT_KEY) is True and live:
            raise LegacyResidentDrainRequired(
                f"legacy Resident Task {task.id} is still live"
            )
        if (
            context.get("strategic_cycle_id")
            and _payload(task).get("type") in LEGACY_CYCLE_TASK_TYPES
            and live
        ):
            raise LegacyResidentDrainRequired(
                f"legacy StrategicCycle Task {task.id} is still live"
            )
