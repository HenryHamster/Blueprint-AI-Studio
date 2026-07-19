"""Authoritative scheduling for bounded Strategic Resident turns."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.artifact_refs import union_resident_artifact_ref_grants
from app.models import Project, StrategicResident, Task, TaskHistory, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.strategic_resident_turns import (
    INPUT_FROZEN_CONTEXT_KEY,
    MUTATION_AUTHORIZED_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    STRATEGIC_RESIDENT_TURN_TASK_TYPE,
    Lane,
    ResidentTurnWake,
    _coalesce_successor_payload,
    _current_checkpoint_identity,
    _ensure_resident_locked,
    _lane_successor_attr,
    _load_pointer_tasks_locked,
    _merge_project_change_cursor,
    _bounded_public_value,
    _sha256,
    _task_payload,
    coalesce_shadow_resident_turn,
)
from app.time_utils import _utcnow
from app.workbench_state import STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY
from app.strategic_resident_capacity import (
    requires_local_runner,
    waiting_for_required_runner,
)

_TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class AuthoritativeResidentOwnershipError(RuntimeError):
    """Authoritative scheduling requires Strategic Resident ownership."""


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(json.dumps(value, default=str))


def is_authoritative_resident_turn(
    task: Task | None,
    *,
    project_id: uuid.UUID | None = None,
    mutable_successor: bool = False,
) -> bool:
    if task is None or task.status in _TERMINAL:
        return False
    if project_id is not None and task.project_id != project_id:
        return False
    payload = _task_payload(task)
    context = task.context if isinstance(task.context, dict) else {}
    if (
        payload.get("type") != STRATEGIC_RESIDENT_TURN_TASK_TYPE
        or payload.get("engine_owner") != "strategic_resident"
        or payload.get("shadow_only") is not False
        or context.get(SHADOW_CONTEXT_KEY) is not False
        or context.get(MUTATION_AUTHORIZED_CONTEXT_KEY) is not True
    ):
        return False
    expected_frozen = not mutable_successor
    return bool(
        context.get(INPUT_FROZEN_CONTEXT_KEY) is expected_frozen
        and (not mutable_successor or task.status == TaskStatus.QUEUED)
    )


def _repair_authoritative_pointers(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
) -> None:
    active = tasks.get(resident.active_turn_task_id)
    if resident.active_turn_task_id is not None and not is_authoritative_resident_turn(
        active, project_id=resident.project_id
    ):
        raise RuntimeError("authoritative Resident active-turn pointer is invalid")
    for lane in ("interactive", "background"):
        attr = _lane_successor_attr(lane)
        pointer = getattr(resident, attr)
        task = tasks.get(pointer)
        if pointer is not None and not is_authoritative_resident_turn(
            task, project_id=resident.project_id, mutable_successor=True
        ):
            raise RuntimeError(
                f"authoritative Resident {lane} successor pointer is invalid"
            )


def promote_authoritative_successor(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
    *,
    checkpoint_version: int | None = None,
    checkpoint_hash: str | None = None,
    authority_generation: int | None = None,
    engine_owner: str = "strategic_resident",
) -> Task | None:
    """Promote interactive before background and freeze its provider input."""

    if resident.active_turn_task_id is not None:
        active = tasks.get(resident.active_turn_task_id)
        return (
            active
            if is_authoritative_resident_turn(
                active, project_id=resident.project_id
            )
            else None
        )
    for lane in ("interactive", "background"):
        attr = _lane_successor_attr(lane)
        task = tasks.get(getattr(resident, attr))
        if not is_authoritative_resident_turn(
            task, project_id=resident.project_id, mutable_successor=True
        ):
            setattr(resident, attr, None)
            continue
        if (checkpoint_version is None) != (checkpoint_hash is None):
            raise ValueError("checkpoint version and hash must be supplied together")
        payload = dict(_task_payload(task))
        if checkpoint_version is not None and checkpoint_hash is not None:
            payload["checkpoint_version"] = int(checkpoint_version)
            payload["checkpoint_hash"] = str(checkpoint_hash)
        payload["authority_generation"] = int(
            resident.authority_generation
            if authority_generation is None
            else authority_generation
        )
        payload["engine_owner"] = engine_owner
        payload["shadow_only"] = False
        task.payloads = [
            _json_safe(
                validate_internal_payload(
                    payload,
                    "strategic_resident_turn:promote_authoritative",
                )
            )
        ]
        context = dict(task.context or {})
        context[INPUT_FROZEN_CONTEXT_KEY] = True
        context[MUTATION_AUTHORIZED_CONTEXT_KEY] = True
        context[SHADOW_CONTEXT_KEY] = False
        context["_resident_authority_generation"] = int(
            resident.authority_generation
            if authority_generation is None
            else authority_generation
        )
        context["_resident_engine_owner_observed"] = engine_owner
        task.context = context
        resident.active_turn_task_id = task.id
        resident.active_turn_lane = lane
        setattr(resident, attr, None)
        return task
    return None


async def _create_authoritative_turn(
    session: AsyncSession,
    resident: StrategicResident,
    *,
    lane: Lane,
    trigger_kind: str,
    message_sequence: int | None,
    project_change_cursor: dict[str, Any] | None,
    input_frozen: bool,
    artifact_ref_grants: frozenset[uuid.UUID],
) -> Task:
    checkpoint_version, checkpoint_hash = await _current_checkpoint_identity(
        session, resident
    )
    if checkpoint_version is None or checkpoint_hash is None:
        raise RuntimeError("authoritative Resident turn requires a checkpoint")
    runner_policy = _bounded_public_value(resident.runner_policy or {})
    provider_policy = _bounded_public_value(resident.provider_policy or {})
    failover_policy = _bounded_public_value(resident.failover_policy or {})
    requested_model = (
        provider_policy.get("model") if isinstance(provider_policy, dict) else None
    )
    requested_provider = (
        provider_policy.get("provider") if isinstance(provider_policy, dict) else None
    )
    payload = {
        "type": STRATEGIC_RESIDENT_TURN_TASK_TYPE,
        "resident_id": str(resident.project_id),
        "lane": lane,
        "authority_generation": int(resident.authority_generation),
        "engine_owner": "strategic_resident",
        "shadow_only": False,
        "trigger_kind": trigger_kind,
        "trigger_count": 1,
        "checkpoint_version": checkpoint_version,
        "checkpoint_hash": checkpoint_hash,
        "manifest_version": "resident-turn-input-v1",
        "resident_runner_policy": runner_policy,
        "resident_provider_policy": provider_policy,
        "resident_failover_policy": failover_policy,
        **({"model": requested_model} if requested_model else {}),
        **({"provider": requested_provider} if requested_provider else {}),
        **(
            {
                "message_sequence_start": int(message_sequence),
                "message_sequence_end": int(message_sequence),
            }
            if message_sequence is not None
            else {}
        ),
        **(
            {
                "project_change_cursor": _merge_project_change_cursor(
                    resident.handled_project_change_cursor,
                    project_change_cursor,
                )
            }
            if resident.handled_project_change_cursor or project_change_cursor
            else {}
        ),
    }
    payload = _json_safe(
        validate_internal_payload(payload, "strategic_resident_turn:authoritative")
    )
    task = Task(
        project_id=resident.project_id,
        title=(
            "Resident operator turn"
            if lane == "interactive"
            else "Resident background turn"
        ),
        description="Bounded managed Strategic Resident reasoning turn",
        payloads=[payload],
        context={
            SHADOW_CONTEXT_KEY: False,
            MUTATION_AUTHORIZED_CONTEXT_KEY: True,
            INPUT_FROZEN_CONTEXT_KEY: input_frozen,
            "_resident_project_id": str(resident.project_id),
            "_resident_lane": lane,
            "_resident_authority_generation": int(resident.authority_generation),
            "_resident_engine_owner_observed": "strategic_resident",
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: requires_local_runner(
                resident
            ),
            "_resident_no_silent_provider_downgrade": True,
            "_resident_policy_lineage": {
                "runner_policy_hash": _sha256(runner_policy),
                "provider_policy_hash": _sha256(provider_policy),
                "failover_policy_hash": _sha256(failover_policy),
            },
        },
        status=TaskStatus.QUEUED,
        priority=95 if lane == "interactive" else 50,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        archived=True,
    )
    union_resident_artifact_ref_grants(task, artifact_ref_grants)
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "STRATEGIC_RESIDENT",
                "lane": lane,
                "bounded_provider_turn": True,
            },
        )
    )
    return task


async def coalesce_authoritative_resident_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    lane: Lane = "interactive",
    trigger_kind: str = "operator_message",
    message_sequence: int | None = None,
    project_change_cursor: dict[str, Any] | None = None,
    locked_project: Project | None = None,
    locked_resident: StrategicResident | None = None,
    prelocked_tasks: dict[uuid.UUID, Task] | None = None,
    artifact_ref_grants: frozenset[uuid.UUID] = frozenset(),
) -> ResidentTurnWake:
    if lane == "interactive" and message_sequence is None:
        raise ValueError("interactive Resident turn requires message_sequence")
    resident = locked_resident
    if resident is None:
        resident = await _ensure_resident_locked(
            session, project_id, locked_project=locked_project
        )
    if resident.project_id != project_id:
        raise ValueError("locked Resident belongs to another project")
    if resident.engine_owner != "strategic_resident":
        raise AuthoritativeResidentOwnershipError(
            "Strategic Resident does not own this project"
        )
    recovery = (
        resident.recovery_state if isinstance(resident.recovery_state, dict) else {}
    )
    if recovery.get("unknown_operation_receipt_ids"):
        return ResidentTurnWake(
            task_id=None,
            state="waiting_for_capacity",
            lane=lane,
            active_task_id=resident.active_turn_task_id,
            successor_task_id=getattr(resident, _lane_successor_attr(lane)),
        )
    if lane == "background" and resident.background_policy == "paused":
        return ResidentTurnWake(
            task_id=None,
            state="paused",
            lane=lane,
            active_task_id=resident.active_turn_task_id,
            successor_task_id=None,
        )
    tasks = (
        prelocked_tasks
        if prelocked_tasks is not None
        else await _load_pointer_tasks_locked(session, resident)
    )
    _repair_authoritative_pointers(resident, tasks)
    active = promote_authoritative_successor(resident, tasks)
    successor_id = None
    if active is None:
        result = await _create_authoritative_turn(
            session,
            resident,
            lane=lane,
            trigger_kind=trigger_kind,
            message_sequence=message_sequence,
            project_change_cursor=project_change_cursor,
            input_frozen=True,
            artifact_ref_grants=artifact_ref_grants,
        )
        resident.active_turn_task_id = result.id
        resident.active_turn_lane = lane
        state: Literal["queued", "coalesced"] = "queued"
    else:
        attr = _lane_successor_attr(lane)
        successor = tasks.get(getattr(resident, attr))
        if not is_authoritative_resident_turn(
            successor, project_id=project_id, mutable_successor=True
        ):
            successor = await _create_authoritative_turn(
                session,
                resident,
                lane=lane,
                trigger_kind=trigger_kind,
                message_sequence=message_sequence,
                project_change_cursor=project_change_cursor,
                input_frozen=False,
                artifact_ref_grants=artifact_ref_grants,
            )
            setattr(resident, attr, successor.id)
        else:
            _coalesce_successor_payload(
                successor,
                message_sequence=message_sequence,
                project_change_cursor=project_change_cursor,
            )
            union_resident_artifact_ref_grants(successor, artifact_ref_grants)
        result = successor
        successor_id = successor.id
        state = "coalesced"
        if resident.active_turn_lane == "background" and lane == "interactive":
            scheduling = dict(resident.scheduling_state or {})
            scheduling.update(
                {
                    "preempt_requested_at": _utcnow().isoformat(),
                    "preempt_requested_generation": int(
                        resident.authority_generation
                    ),
                    "preempt_for_task_id": str(successor.id),
                }
            )
            resident.scheduling_state = scheduling
    now = _utcnow()
    resident.last_wakeup_at = now
    scheduling = dict(resident.scheduling_state or {})
    scheduling.update(
        {
            "last_enqueue_at": now.isoformat(),
            "last_lane": lane,
            "last_state": state,
            "active_task_id": str(resident.active_turn_task_id),
        }
    )
    resident.scheduling_state = scheduling
    session.add(resident)
    waiting_for_capacity = await waiting_for_required_runner(session, resident)
    return ResidentTurnWake(
        task_id=result.id,
        state="waiting_for_capacity" if waiting_for_capacity else state,
        lane=lane,
        active_task_id=resident.active_turn_task_id,
        successor_task_id=successor_id,
    )


async def coalesce_resident_turn(
    session: AsyncSession,
    **kwargs: Any,
) -> ResidentTurnWake:
    resident = kwargs.get("locked_resident")
    if resident is None:
        resident = await _ensure_resident_locked(
            session,
            kwargs["project_id"],
            locked_project=kwargs.get("locked_project"),
        )
        kwargs["locked_resident"] = resident
    if resident.engine_owner == "legacy":
        shadow_kwargs = dict(kwargs)
        shadow_kwargs.pop("artifact_ref_grants", None)
        return await coalesce_shadow_resident_turn(session, **shadow_kwargs)
    return await coalesce_authoritative_resident_turn(session, **kwargs)


__all__ = [
    "AuthoritativeResidentOwnershipError",
    "coalesce_authoritative_resident_turn",
    "coalesce_resident_turn",
    "is_authoritative_resident_turn",
    "promote_authoritative_successor",
]
