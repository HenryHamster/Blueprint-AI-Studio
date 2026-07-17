"""Atomic ownership cutover and rollback for the Strategic Resident engine."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import (
    ProjectMessage,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_resident_cleanup_state import unsettled_cleanup_descriptors
from app.strategic_resident_cutover_messages import (
    release_cancelled_shadow_message_claims,
)
from app.strategic_resident_checkpoint_validation import (
    ResidentCheckpointIntegrityError,
    verify_locked_current_checkpoint,
)
from app.strategic_resident_turns import (
    INPUT_FROZEN_CONTEXT_KEY,
    MUTATION_AUTHORIZED_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    STRATEGIC_RESIDENT_TURN_TASK_TYPE,
)
from app.strategic_resident_cutover_turn import seed_cutover_conversation_turn
from app.strategic_resident_legacy_projection import (
    LegacyCheckpointProjectionError,
    project_checkpoint_to_legacy,
)
from app.strategic_resident_cutover_boundary import (
    CUTOVER_CHECKPOINT_AUTHORIZATION_KEY,
    append_live_cutover_checkpoint,
)
from app.strategic_resident_transition_locks import (
    LegacyResidentDrainRequired,
    ResidentCheckpointBoundaryError,
    ResidentOwnershipStateError,
    ResidentOwnershipTransitionError,
    UnreconciledResidentOperations,
    assert_legacy_resident_drained as _assert_legacy_resident_drained,
    lock_project_resident_tasks as _lock_project_resident_tasks,
)
from app.time_utils import _utcnow


LEGACY_ENGINE_OWNER = "legacy"
STRATEGIC_ENGINE_OWNER = "strategic_resident"
_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
_UNSETTLED_RECEIPT_STATUSES = frozenset(
    {"pending", "applying", "unknown_effect", "reconciling"}
)
@dataclass(frozen=True)
class ResidentOwnershipTransition:
    project_id: uuid.UUID
    previous_owner: str
    new_owner: str
    previous_generation: int
    new_generation: int
    checkpoint_id: uuid.UUID
    checkpoint_version: int
    checkpoint_hash: str
    cancelled_task_ids: tuple[uuid.UUID, ...]
    already_owned: bool
    legacy_projection_applied: bool


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _task_payload(task: Task) -> dict[str, Any]:
    payload = task.first_payload
    return payload if isinstance(payload, dict) else {}


def _is_resident_turn(task: Task) -> bool:
    return _task_payload(task).get("type") == STRATEGIC_RESIDENT_TURN_TASK_TYPE


def _is_shadow_turn(task: Task) -> bool:
    context = task.context if isinstance(task.context, dict) else {}
    payload = _task_payload(task)
    return bool(
        context.get(SHADOW_CONTEXT_KEY) is True or payload.get("shadow_only") is True
    )


def _is_terminal(task: Task) -> bool:
    return _enum_value(task.status) in {
        _enum_value(status) for status in _TERMINAL_TASK_STATUSES
    }


async def _current_checkpoint(
    session: AsyncSession,
    resident: StrategicResident,
) -> StrategicResidentCheckpoint:
    if resident.current_checkpoint_id is None:
        raise ResidentCheckpointBoundaryError(
            "Resident has no current checkpoint boundary"
        )
    checkpoint = (
        await session.execute(
            select(StrategicResidentCheckpoint).where(
                StrategicResidentCheckpoint.id == resident.current_checkpoint_id,
                StrategicResidentCheckpoint.project_id == resident.project_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        raise ResidentCheckpointBoundaryError(
            "Resident current checkpoint is missing or cross-project"
        )
    try:
        await verify_locked_current_checkpoint(session, resident, checkpoint)
    except ResidentCheckpointIntegrityError as exc:
        raise ResidentCheckpointBoundaryError(str(exc)) from exc
    return checkpoint


def _assert_pointer_lineage(
    resident: StrategicResident,
    tasks: list[Task],
    *,
    owner: str,
    checkpoint: StrategicResidentCheckpoint,
) -> None:
    tasks_by_id = {task.id: task for task in tasks}
    roles = (
        ("active_turn_task_id", resident.active_turn_lane, True),
        ("interactive_successor_task_id", "interactive", False),
        ("background_successor_task_id", "background", False),
    )
    expected_shadow = owner == LEGACY_ENGINE_OWNER
    for attr, expected_lane, expected_frozen in roles:
        task_id = getattr(resident, attr)
        if task_id is None:
            continue
        task = tasks_by_id[task_id]
        payload = _task_payload(task)
        context = task.context if isinstance(task.context, dict) else {}
        if _is_terminal(task):
            raise ResidentOwnershipStateError(
                f"Resident {attr} references a terminal turn"
            )
        if expected_lane not in {"interactive", "background"}:
            raise ResidentOwnershipStateError(
                "Resident active turn lane is missing or malformed"
            )
        if (
            str(payload.get("resident_id") or "") != str(resident.project_id)
            or str(context.get("_resident_project_id") or "")
            != str(resident.project_id)
        ):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has stale project lineage"
            )
        try:
            payload_generation = int(payload.get("authority_generation"))
            context_generation = int(
                context.get("_resident_authority_generation")
            )
        except (TypeError, ValueError):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has missing authority lineage"
            ) from None
        if (
            payload_generation != int(resident.authority_generation)
            or context_generation != int(resident.authority_generation)
            or str(payload.get("engine_owner") or "") != owner
            or str(context.get("_resident_engine_owner_observed") or "") != owner
        ):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has stale owner or generation lineage"
            )
        if (
            int(payload.get("checkpoint_version") or 0) != int(checkpoint.version)
            or str(payload.get("checkpoint_hash") or "")
            != str(checkpoint.content_hash)
        ):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has stale checkpoint lineage"
            )
        if (
            str(payload.get("lane") or "") != expected_lane
            or str(context.get("_resident_lane") or "") != expected_lane
        ):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has inconsistent lane lineage"
            )
        if context.get(INPUT_FROZEN_CONTEXT_KEY) is not expected_frozen:
            raise ResidentOwnershipStateError(
                f"Resident {attr} has invalid input-freeze state"
            )
        if attr != "active_turn_task_id" and task.status != TaskStatus.QUEUED:
            raise ResidentOwnershipStateError(
                f"Resident {attr} is not an unclaimed queued successor"
            )
        if _is_shadow_turn(task) is not expected_shadow:
            expected = "shadow" if expected_shadow else "authoritative"
            raise ResidentOwnershipStateError(
                f"Resident {attr} is not a {expected} turn"
            )
        if (
            context.get(SHADOW_CONTEXT_KEY) is not expected_shadow
            or context.get(MUTATION_AUTHORIZED_CONTEXT_KEY) is expected_shadow
            or payload.get("shadow_only") is not expected_shadow
        ):
            raise ResidentOwnershipStateError(
                f"Resident {attr} has inconsistent authority flags"
            )


async def _next_meaningful_wakeup(
    session: AsyncSession,
    resident: StrategicResident,
    *,
    now: datetime,
) -> datetime | None:
    pending_sequence = await session.scalar(
        select(func.max(ProjectMessage.sequence)).where(
            ProjectMessage.project_id == resident.project_id,
            ProjectMessage.surface == "conversation",
            ProjectMessage.from_type == "user",
            ProjectMessage.handling_state.in_(["pending", "claimed"]),
            ProjectMessage.sequence > int(resident.handled_message_sequence),
        )
    )
    if pending_sequence is not None:
        return now
    scheduling = (
        resident.scheduling_state
        if isinstance(resident.scheduling_state, dict)
        else {}
    )
    if scheduling.get("pending_project_change_cursor"):
        return now
    raw_deadline = scheduling.get("deadline_at")
    if isinstance(raw_deadline, str) and raw_deadline:
        try:
            deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            if deadline.tzinfo is not None:
                deadline = deadline.astimezone(timezone.utc).replace(tzinfo=None)
            return deadline
        except ValueError:
            raise ResidentOwnershipStateError(
                "Resident scheduling deadline is malformed"
            ) from None
    return None


def _cancel_task(
    task: Task,
    *,
    now: datetime,
    generation: int,
    transition: str,
) -> None:
    if _is_terminal(task):
        return
    context = dict(task.context or {})
    context.update(
        {
            "_resident_superseded_by_generation": generation,
            "_resident_superseded_by_transition": transition,
            "_resident_superseded_at": now.isoformat(),
            INPUT_FROZEN_CONTEXT_KEY: True,
            MUTATION_AUTHORIZED_CONTEXT_KEY: False,
        }
    )
    task.context = context
    task.status = TaskStatus.CANCELLED
    task.completed_at = now
    task.updated_at = now
    task.latest_status_message = (
        f"Resident turn superseded by {transition} at authority generation {generation}"
    )


def _transition_result(
    resident: StrategicResident,
    checkpoint: StrategicResidentCheckpoint,
    *,
    previous_owner: str,
    previous_generation: int,
    cancelled: list[uuid.UUID],
    already_owned: bool,
    legacy_projection_applied: bool,
) -> ResidentOwnershipTransition:
    return ResidentOwnershipTransition(
        project_id=resident.project_id,
        previous_owner=previous_owner,
        new_owner=resident.engine_owner,
        previous_generation=previous_generation,
        new_generation=int(resident.authority_generation),
        checkpoint_id=checkpoint.id,
        checkpoint_version=int(checkpoint.version),
        checkpoint_hash=checkpoint.content_hash,
        cancelled_task_ids=tuple(cancelled),
        already_owned=already_owned,
        legacy_projection_applied=legacy_projection_applied,
    )


async def cutover_to_strategic_resident(
    source: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ResidentOwnershipTransition:
    """Atomically fence legacy shadow work and grant the Resident engine ownership."""

    effective_now = now or _utcnow()
    if isinstance(source, AsyncSession):
        raise TypeError(
            "cutover_to_strategic_resident owns its transaction; "
            "pass an async_sessionmaker, not AsyncSession"
        )
    async with source() as session, session.begin():
        project, resident, tasks = await _lock_project_resident_tasks(
            session,
            project_id,
            reject_active_legacy_cycles=True,
        )
        checkpoint = await _current_checkpoint(session, resident)
        previous_owner = _enum_value(resident.engine_owner)
        previous_engine_version = str(resident.engine_version)
        previous_generation = int(resident.authority_generation)
        _assert_pointer_lineage(
            resident,
            tasks,
            owner=previous_owner,
            checkpoint=checkpoint,
        )
        if previous_owner == STRATEGIC_ENGINE_OWNER:
            stranded = [
                task.id
                for task in tasks
                if _is_resident_turn(task)
                and _is_shadow_turn(task)
                and not _is_terminal(task)
            ]
            if stranded:
                raise ResidentOwnershipStateError(
                    "authoritative Resident has stranded shadow turns: "
                    + ", ".join(str(item) for item in sorted(stranded, key=str))
                )
            return _transition_result(
                resident,
                checkpoint,
                previous_owner=previous_owner,
                previous_generation=previous_generation,
                cancelled=[],
                already_owned=True,
                legacy_projection_applied=False,
            )
        if previous_owner != LEGACY_ENGINE_OWNER:
            raise ResidentOwnershipStateError(
                f"unsupported Resident engine owner: {previous_owner}"
            )
        unsettled_cleanup = unsettled_cleanup_descriptors(resident)
        if unsettled_cleanup:
            raise ResidentOwnershipStateError(
                "rollback cleanup is not settled; recutover is fenced: "
                + ", ".join(unsettled_cleanup)
            )
        _assert_legacy_resident_drained(project, tasks)

        cancelled: list[uuid.UUID] = []
        next_generation = previous_generation + 1
        for task in tasks:
            if _is_resident_turn(task) and not _is_shadow_turn(task):
                raise ResidentOwnershipStateError(
                    "legacy ownership contains a mutation-authorized Resident turn"
                )
            if (
                _is_resident_turn(task)
                and _is_shadow_turn(task)
                and not _is_terminal(task)
            ):
                _cancel_task(
                    task,
                    now=effective_now,
                    generation=next_generation,
                    transition="legacy_to_strategic_resident",
                )
                cancelled.append(task.id)

        boundary = await append_live_cutover_checkpoint(
            session,
            project=project,
            resident=resident,
            current=checkpoint,
            next_generation=next_generation,
            cutover_at=effective_now,
        )
        await release_cancelled_shadow_message_claims(
            session,
            project_id=project_id,
            cancelled_shadow_task_ids=cancelled,
        )
        resident.previous_checkpoint_id = checkpoint.id
        resident.current_checkpoint_id = boundary.checkpoint.id
        resident.handled_message_sequence = boundary.handled_message_sequence
        resident.handled_project_change_cursor = (
            boundary.handled_project_change_cursor
        )
        checkpoint = boundary.checkpoint
        resident.active_turn_task_id = None
        resident.active_turn_lane = None
        resident.interactive_successor_task_id = None
        resident.background_successor_task_id = None
        resident.engine_owner = STRATEGIC_ENGINE_OWNER
        resident.engine_version = "strategic-resident-v1"
        resident.authority_generation = next_generation
        resident.health_status = "recovering"
        resident.health_state = {
            **dict(resident.health_state or {}),
            "ownership_transition": "cutover_complete",
            "authority_generation": next_generation,
            "checkpoint_id": str(checkpoint.id),
            "transitioned_at": effective_now.isoformat(),
        }
        resident.recovery_state = {}
        resident.rollout_state = {
            **{
                key: deepcopy(value)
                for key, value in dict(resident.rollout_state or {}).items()
                if key != CUTOVER_CHECKPOINT_AUTHORIZATION_KEY
            },
            "shadow": False,
            "engine_owner": STRATEGIC_ENGINE_OWNER,
            "cutover_completed_at": effective_now.isoformat(),
            "cutover_checkpoint_id": str(checkpoint.id),
            "cutover_checkpoint_version": int(checkpoint.version),
            "cutover_checkpoint_hash": checkpoint.content_hash,
            "cutover_cancelled_shadow_task_ids": [str(item) for item in cancelled],
            "pre_cutover_engine_version": previous_engine_version,
        }
        resident.scheduling_state = {
            **{
                key: deepcopy(value)
                for key, value in dict(resident.scheduling_state or {}).items()
                if not str(key).startswith(("shadow_", "preempt_"))
            },
            "ownership_transition": "cutover_complete",
            "authority_generation": next_generation,
            "transitioned_at": effective_now.isoformat(),
        }
        if (
            boundary.pending_message_sequence_start is not None
            and boundary.pending_message_sequence_end is not None
        ):
            seeded_turn = await seed_cutover_conversation_turn(
                session,
                resident,
                message_sequence_start=boundary.pending_message_sequence_start,
                message_sequence_end=boundary.pending_message_sequence_end,
            )
            resident.scheduling_state = {
                **dict(resident.scheduling_state or {}),
                "cutover_pending_message_task_id": str(seeded_turn.id),
                "cutover_pending_message_sequence_start": (
                    boundary.pending_message_sequence_start
                ),
                "cutover_pending_message_sequence_end": (
                    boundary.pending_message_sequence_end
                ),
            }
        resident.next_wakeup_at = (
            await _next_meaningful_wakeup(
                session,
                resident,
                now=effective_now,
            )
            if resident.background_policy == "active"
            else None
        )
        resident.updated_at = effective_now
        session.add(resident)
        return _transition_result(
            resident,
            checkpoint,
            previous_owner=previous_owner,
            previous_generation=previous_generation,
            cancelled=sorted(cancelled, key=str),
            already_owned=False,
            legacy_projection_applied=False,
        )


async def rollback_to_legacy_resident(
    source: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ResidentOwnershipTransition:
    """Fence the current generation and restore bounded legacy strategic inputs."""

    effective_now = now or _utcnow()
    if isinstance(source, AsyncSession):
        raise TypeError(
            "rollback_to_legacy_resident owns its transaction; "
            "pass an async_sessionmaker, not AsyncSession"
        )
    transition: ResidentOwnershipTransition
    async with source() as session, session.begin():
        project, resident, tasks = await _lock_project_resident_tasks(
            session, project_id
        )
        checkpoint = await _current_checkpoint(session, resident)
        previous_owner = _enum_value(resident.engine_owner)
        previous_generation = int(resident.authority_generation)
        _assert_pointer_lineage(
            resident,
            tasks,
            owner=previous_owner,
            checkpoint=checkpoint,
        )
        if previous_owner == LEGACY_ENGINE_OWNER:
            transition = _transition_result(
                resident,
                checkpoint,
                previous_owner=previous_owner,
                previous_generation=previous_generation,
                cancelled=[],
                already_owned=True,
                legacy_projection_applied=False,
            )
        elif previous_owner != STRATEGIC_ENGINE_OWNER:
            raise ResidentOwnershipStateError(
                f"unsupported Resident engine owner: {previous_owner}"
            )
        else:
            unsettled = (
                (
                    await session.execute(
                        select(StrategicOperationReceipt)
                        .where(
                            StrategicOperationReceipt.project_id == project_id,
                            StrategicOperationReceipt.status.in_(
                                list(_UNSETTLED_RECEIPT_STATUSES)
                            ),
                        )
                        .order_by(
                            StrategicOperationReceipt.started_at.asc(),
                            StrategicOperationReceipt.id.asc(),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if unsettled:
                raise UnreconciledResidentOperations([row.id for row in unsettled])

            try:
                project_checkpoint_to_legacy(
                    project,
                    checkpoint,
                    now=effective_now,
                )
            except LegacyCheckpointProjectionError as exc:
                raise ResidentCheckpointBoundaryError(str(exc)) from exc
            cancelled: list[uuid.UUID] = []
            next_generation = previous_generation + 1
            cleanup_items: list[dict[str, Any]] = []
            for task in tasks:
                if (
                    _is_resident_turn(task)
                    and _is_shadow_turn(task)
                    and not _is_terminal(task)
                ):
                    raise ResidentOwnershipStateError(
                        "authoritative ownership contains a shadow Resident turn"
                    )
                if _is_resident_turn(task) and not _is_terminal(task):
                    cleanup_items.append(
                        {
                            "task_id": str(task.id),
                            "vm_instance_id": task.vm_instance_id,
                            "account_id": task.llm_account_id
                            or (task.context or {}).get("max_plan_account_id"),
                            "status": "pending",
                            "authority_generation": next_generation,
                            "attempt_count": 0,
                            "recorded_at": effective_now.isoformat(),
                        }
                    )
                    _cancel_task(
                        task,
                        now=effective_now,
                        generation=next_generation,
                        transition="strategic_resident_to_legacy",
                    )
                    cancelled.append(task.id)

            rollout = dict(resident.rollout_state or {})
            legacy_engine_version = str(
                rollout.get("pre_cutover_engine_version") or "legacy-v1"
            )[:64]
            resident.active_turn_task_id = None
            resident.active_turn_lane = None
            resident.interactive_successor_task_id = None
            resident.background_successor_task_id = None
            resident.engine_owner = LEGACY_ENGINE_OWNER
            resident.engine_version = legacy_engine_version
            resident.authority_generation = next_generation
            resident.health_status = "recovering" if cleanup_items else "unknown"
            resident.health_state = {
                "ownership_transition": "rollback_complete",
                "authority_generation": next_generation,
                "checkpoint_id": str(checkpoint.id),
                "transitioned_at": effective_now.isoformat(),
            }
            resident.recovery_state = {
                "rollback_completed_at": effective_now.isoformat(),
                "rollback_checkpoint_id": str(checkpoint.id),
                "cancelled_authoritative_task_ids": [
                    str(item) for item in cancelled
                ],
                "task_cleanup": cleanup_items,
            }
            resident.rollout_state = {
                **rollout,
                "shadow": True,
                "engine_owner": LEGACY_ENGINE_OWNER,
                "rollback_completed_at": effective_now.isoformat(),
                "rollback_checkpoint_id": str(checkpoint.id),
                "rollback_checkpoint_version": int(checkpoint.version),
                "rollback_checkpoint_hash": checkpoint.content_hash,
            }
            resident.scheduling_state = {
                **{
                    key: deepcopy(value)
                    for key, value in dict(
                        resident.scheduling_state or {}
                    ).items()
                    if not str(key).startswith("cutover_pending_message_")
                },
                "ownership_transition": "rollback_complete",
                "authority_generation": next_generation,
                "transitioned_at": effective_now.isoformat(),
            }
            resident.next_wakeup_at = None
            resident.updated_at = effective_now
            session.add(project)
            session.add(resident)
            transition = _transition_result(
                resident,
                checkpoint,
                previous_owner=previous_owner,
                previous_generation=previous_generation,
                cancelled=sorted(cancelled, key=str),
                already_owned=False,
                legacy_projection_applied=True,
            )
    from app.strategic_resident_cleanup import run_resident_transition_cleanup

    await run_resident_transition_cleanup(source, project_id)
    return transition


__all__ = [
    "LegacyResidentDrainRequired",
    "ResidentCheckpointBoundaryError",
    "ResidentOwnershipStateError",
    "ResidentOwnershipTransition",
    "ResidentOwnershipTransitionError",
    "UnreconciledResidentOperations",
    "cutover_to_strategic_resident",
    "rollback_to_legacy_resident",
]
