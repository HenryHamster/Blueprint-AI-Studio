"""Locked task inventory for Strategic Resident ownership transitions."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

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


async def lock_project_resident_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    reject_active_legacy_cycles: bool = False,
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
    if active_cycles and reject_active_legacy_cycles:
        raise LegacyResidentDrainRequired(
            "legacy StrategicCycle work is still active: "
            + ", ".join(cycle.id for cycle in active_cycles)
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
    pointer_ids = sorted(
        {
            task_id
            for task_id in (
                resident.active_turn_task_id,
                resident.interactive_successor_task_id,
                resident.background_successor_task_id,
                legacy_task_id,
            )
            if task_id is not None
        },
        key=str,
    )
    tasks: list[Task] = []
    if pointer_ids:
        tasks.extend(
            (
                await session.execute(
                    select(Task)
                    .where(Task.id.in_(pointer_ids))
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
    if pointer_ids:
        query = query.where(Task.id.not_in(pointer_ids))
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
