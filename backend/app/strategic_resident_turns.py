"""Bounded shadow runtime for the durable Strategic Resident.

This module deliberately performs no model calls and grants no mutation
authority.  While ``StrategicResident.engine_owner == "legacy"`` it mirrors
operator wake/coalescing decisions into hidden backend-managed Tasks so the new
runtime can be compared without competing with the legacy strategist.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Project,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.strategic_resident_checkpointing import (
    initialize_strategic_resident as _initialize_strategic_resident,
)
from app.time_utils import _utcnow

STRATEGIC_RESIDENT_TURN_TASK_TYPE = "strategic_resident_turn"
SHADOW_CONTEXT_KEY = "_resident_shadow_only"
MUTATION_AUTHORIZED_CONTEXT_KEY = "_resident_mutation_authorized"
INPUT_FROZEN_CONTEXT_KEY = "_resident_input_frozen"

Lane = Literal["interactive", "background"]

_MONOTONIC_PROJECT_CURSOR_KEYS = frozenset(
    {"managed_task_terminal", "qualification_verdict", "strategic_cycle_terminal"}
)
_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


@dataclass(frozen=True)
class ResidentTurnWake:
    task_id: uuid.UUID | None
    state: Literal["queued", "coalesced", "paused", "waiting_for_capacity"]
    lane: Lane
    active_task_id: uuid.UUID | None
    successor_task_id: uuid.UUID | None


class ShadowResidentOwnershipError(RuntimeError):
    """Shadow scheduling is invalid once the new engine owns the project."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _json_safe_payload(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, default=str))


def _merge_project_change_cursor(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, int | str | None]:
    """Merge a bounded cursor object without regressing numeric high-waters."""

    merged: dict[str, int | str | None] = {}
    for source in (current or {}, incoming or {}):
        for key in sorted(source, key=str)[:16]:
            if not isinstance(key, str) or not key or len(key) > 80:
                continue
            value = source[key]
            if isinstance(value, bool) or (
                value is not None and not isinstance(value, (int, str))
            ):
                continue
            if isinstance(value, int):
                if value < 0:
                    continue
                prior = merged.get(key)
                merged[key] = max(prior, value) if isinstance(prior, int) else value
            elif isinstance(value, str):
                if len(value) <= 160:
                    prior = merged.get(key)
                    if key in _MONOTONIC_PROJECT_CURSOR_KEYS and isinstance(
                        prior, str
                    ):
                        merged[key] = _newer_durable_cursor(prior, value)
                    else:
                        # Other string cursors are opaque revisions/hashes. Their
                        # producers serialize arrival order under the Resident lock.
                        merged[key] = value
            else:
                merged[key] = None
    return dict(sorted(merged.items())[:16])


def _durable_cursor_order(value: str) -> tuple[int, str] | None:
    """Parse current v2 or legacy ISO durable-event cursors for comparison."""

    if value.startswith("v2:"):
        parts = value.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            return int(parts[1]), f"{parts[2]}:{parts[3]}"
        return None
    # Legacy cursor: ``<ISO timestamp>:<row id>:<outcome>``. Naive DB timestamps
    # use either seconds precision (19 chars) or microseconds (26 chars).
    timestamp_size = 26 if len(value) > 19 and value[19] == "." else 19
    if len(value) <= timestamp_size or value[timestamp_size] != ":":
        return None
    try:
        observed_at = datetime.fromisoformat(value[:timestamp_size])
    except ValueError:
        return None
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    elapsed = observed_at - epoch
    micros = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    return micros, value[timestamp_size + 1 :]


def _newer_durable_cursor(current: str, incoming: str) -> str:
    current_order = _durable_cursor_order(current)
    incoming_order = _durable_cursor_order(incoming)
    if incoming_order is None:
        return current
    if current_order is None:
        return incoming
    return incoming if incoming_order > current_order else current


def _bounded_public_value(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    """Bound legacy JSON before hashing/checkpointing (no private keys)."""

    remaining = budget if budget is not None else [2_000]
    if remaining[0] <= 0 or depth >= 8:
        return "[truncated]"
    remaining[0] -= 1
    if isinstance(value, dict):
        result = {}
        for key in sorted(value, key=str):
            text_key = str(key)
            if text_key.startswith("_"):
                continue
            result[text_key[:160]] = _bounded_public_value(
                value[key], depth=depth + 1, budget=remaining
            )
            if remaining[0] <= 0:
                break
        return result
    if isinstance(value, list):
        return [
            _bounded_public_value(item, depth=depth + 1, budget=remaining)
            for item in value[:200]
            if remaining[0] > 0
        ]
    if isinstance(value, str):
        return value[:8_000]
    return value


def _public_vision(value: Any) -> Any:
    return _bounded_public_value(value)


def _task_payload(task: Task | None) -> dict[str, Any]:
    if task is None:
        return {}
    payload = task.first_payload
    return payload if isinstance(payload, dict) else {}


def _is_resident_turn_task(
    task: Task | None,
    *,
    project_id: uuid.UUID,
) -> bool:
    return bool(
        task is not None
        and task.project_id == project_id
        and _task_payload(task).get("type") == STRATEGIC_RESIDENT_TURN_TASK_TYPE
    )


def _is_live_pointer_task(
    task: Task | None,
    *,
    project_id: uuid.UUID,
) -> bool:
    return bool(
        _is_resident_turn_task(task, project_id=project_id)
        and task is not None
        and task.status not in _TERMINAL_TASK_STATUSES
    )


def _is_valid_active_task(task: Task | None, *, project_id: uuid.UUID) -> bool:
    context = (
        task.context if task is not None and isinstance(task.context, dict) else {}
    )
    return bool(
        _is_live_pointer_task(task, project_id=project_id)
        and context.get(SHADOW_CONTEXT_KEY) is True
        and context.get(MUTATION_AUTHORIZED_CONTEXT_KEY) is False
        and context.get(INPUT_FROZEN_CONTEXT_KEY) is True
    )


def _is_mutable_successor_task(task: Task | None, *, project_id: uuid.UUID) -> bool:
    context = (
        task.context if task is not None and isinstance(task.context, dict) else {}
    )
    return bool(
        _is_live_pointer_task(task, project_id=project_id)
        and task is not None
        and task.status == TaskStatus.QUEUED
        and context.get(SHADOW_CONTEXT_KEY) is True
        and context.get(MUTATION_AUTHORIZED_CONTEXT_KEY) is False
        and context.get(INPUT_FROZEN_CONTEXT_KEY) is False
    )


def _lane_successor_attr(lane: Lane) -> str:
    return (
        "interactive_successor_task_id"
        if lane == "interactive"
        else "background_successor_task_id"
    )


async def _load_pointer_tasks_locked(
    session: AsyncSession,
    resident: StrategicResident,
) -> dict[uuid.UUID, Task]:
    ids = {
        pointer
        for pointer in (
            resident.active_turn_task_id,
            resident.interactive_successor_task_id,
            resident.background_successor_task_id,
        )
        if pointer is not None
    }
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Task)
                .where(Task.id.in_(ids))
                .order_by(Task.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


def _repair_pointer_state(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
) -> None:
    """Clear missing, terminal, cross-project, or wrong-type pointers."""

    active = tasks.get(resident.active_turn_task_id)
    if not _is_valid_active_task(active, project_id=resident.project_id):
        resident.active_turn_task_id = None
        resident.active_turn_lane = None

    for lane in ("interactive", "background"):
        attr = _lane_successor_attr(lane)
        pointer = getattr(resident, attr)
        task = tasks.get(pointer)
        if not _is_mutable_successor_task(task, project_id=resident.project_id):
            setattr(resident, attr, None)


def _promote_successor(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
) -> Task | None:
    """Promote interactive before background when no global turn is active."""

    if resident.active_turn_task_id is not None:
        return tasks.get(resident.active_turn_task_id)
    for lane in ("interactive", "background"):
        attr = _lane_successor_attr(lane)
        pointer = getattr(resident, attr)
        task = tasks.get(pointer)
        if not _is_mutable_successor_task(task, project_id=resident.project_id):
            setattr(resident, attr, None)
            continue
        resident.active_turn_task_id = task.id
        resident.active_turn_lane = lane
        setattr(resident, attr, None)
        context = dict(task.context or {})
        context[INPUT_FROZEN_CONTEXT_KEY] = True
        task.context = context
        return task
    return None


async def _ensure_resident_locked(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    locked_project: Project | None = None,
) -> StrategicResident:
    return await _initialize_strategic_resident(
        session, project_id, locked_project=locked_project
    )


async def initialize_strategic_resident(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    locked_project: Project | None = None,
) -> StrategicResident:
    """Compatibility facade for the canonical checkpoint initializer."""

    return await _initialize_strategic_resident(
        session, project_id, locked_project=locked_project
    )


async def lock_shadow_resident_turn_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    locked_project: Project,
) -> tuple[StrategicResident, dict[uuid.UUID, Task]]:
    """Lock all existing rows a shadow coalesce may mutate.

    The caller can do this before reserving a conversation sequence so Resident
    or Task-pointer contention never convoys the per-project message counter.
    Holding the Resident lock stabilizes its pointer set while the Task rows are
    acquired in deterministic ID order.
    """

    resident = await _ensure_resident_locked(
        session,
        project_id,
        locked_project=locked_project,
    )
    return resident, await _load_pointer_tasks_locked(session, resident)


async def _current_checkpoint_identity(
    session: AsyncSession,
    resident: StrategicResident,
) -> tuple[int | None, str | None]:
    if resident.current_checkpoint_id is None:
        return None, None
    row = await session.get(
        StrategicResidentCheckpoint,
        resident.current_checkpoint_id,
    )
    if row is None or row.project_id != resident.project_id:
        return None, None
    return int(row.version), str(row.content_hash)


def _new_turn_context(
    resident: StrategicResident,
    lane: Lane,
    *,
    input_frozen: bool,
) -> dict[str, Any]:
    return {
        SHADOW_CONTEXT_KEY: True,
        MUTATION_AUTHORIZED_CONTEXT_KEY: False,
        INPUT_FROZEN_CONTEXT_KEY: input_frozen,
        "_resident_project_id": str(resident.project_id),
        "_resident_lane": lane,
        "_resident_authority_generation": int(resident.authority_generation),
        "_resident_engine_owner_observed": resident.engine_owner,
    }


async def _create_turn_task(
    session: AsyncSession,
    resident: StrategicResident,
    *,
    lane: Lane,
    trigger_kind: str,
    message_sequence: int | None,
    project_change_cursor: dict[str, Any] | None,
    input_frozen: bool,
) -> Task:
    checkpoint_version, checkpoint_hash = await _current_checkpoint_identity(
        session, resident
    )
    payload = {
        "type": STRATEGIC_RESIDENT_TURN_TASK_TYPE,
        # StrategicResident is project-keyed; its durable identity is project_id.
        "resident_id": str(resident.project_id),
        "lane": lane,
        "authority_generation": int(resident.authority_generation),
        "engine_owner": resident.engine_owner,
        "shadow_only": True,
        "trigger_kind": trigger_kind,
        "trigger_count": 1,
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
        **(
            {
                "checkpoint_version": checkpoint_version,
                "checkpoint_hash": checkpoint_hash,
            }
            if checkpoint_version is not None and checkpoint_hash is not None
            else {}
        ),
        "manifest_version": "resident-turn-input-v1",
    }
    payload = _json_safe_payload(
        validate_internal_payload(payload, "strategic_resident_turn:shadow")
    )
    task = Task(
        project_id=resident.project_id,
        title=(
            "Resident operator turn (shadow)"
            if lane == "interactive"
            else "Resident background turn (shadow)"
        ),
        description="Backend-only bounded Resident runtime comparison; zero model calls",
        payloads=[payload],
        context=_new_turn_context(resident, lane, input_frozen=input_frozen),
        status=TaskStatus.QUEUED,
        priority=95 if lane == "interactive" else 50,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        # Shadow control-plane Tasks are intentionally absent from default task
        # and activity projections. The durable Resident pointers + scheduling
        # state are their operator-facing home.
        archived=True,
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "STRATEGIC_RESIDENT_SHADOW",
                "lane": lane,
                "zero_model_calls": True,
            },
        )
    )
    return task


def _coalesce_successor_payload(
    task: Task,
    *,
    message_sequence: int | None,
    project_change_cursor: dict[str, Any] | None,
) -> None:
    payload = dict(_task_payload(task))
    payload["trigger_count"] = min(10_000, int(payload.get("trigger_count") or 1) + 1)
    if message_sequence is not None:
        start = payload.get("message_sequence_start")
        payload["message_sequence_start"] = min(
            int(start) if isinstance(start, int) else int(message_sequence),
            int(message_sequence),
        )
        payload["message_sequence_end"] = max(
            int(payload.get("message_sequence_end") or message_sequence),
            int(message_sequence),
        )
    if project_change_cursor:
        payload["project_change_cursor"] = _merge_project_change_cursor(
            payload.get("project_change_cursor")
            if isinstance(payload.get("project_change_cursor"), dict)
            else {},
            project_change_cursor,
        )
    # Only an unclaimed successor is mutable. Once promoted to active the
    # coalescer never calls this helper for it, preserving immutable turn input.
    task.payloads = [
        _json_safe_payload(
            validate_internal_payload(payload, "strategic_resident_turn:coalesce")
        )
    ]
    task.updated_at = _utcnow()


async def coalesce_shadow_resident_turn(
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
) -> ResidentTurnWake:
    """Create/coalesce one backend-managed shadow turn without committing.

    Caller lock order is required: Project -> StrategicResident -> Task pointers
    -> ProjectMessageSequence. ``prelocked_tasks`` is used by interactive sends
    to keep all potentially blocking row locks outside the sequence window.
    """

    if lane == "interactive" and message_sequence is None:
        raise ValueError("interactive Resident turn requires message_sequence")

    resident = locked_resident
    if resident is not None and resident.project_id != project_id:
        raise ValueError("locked Resident belongs to a different project")
    if prelocked_tasks is not None and resident is None:
        raise ValueError("prelocked Tasks require a locked Resident")
    if resident is None:
        resident = await _ensure_resident_locked(
            session, project_id, locked_project=locked_project
        )
    if resident.engine_owner != "legacy":
        raise ShadowResidentOwnershipError(
            "shadow Resident turns are disabled after strategic engine ownership flips"
        )
    if lane == "background" and resident.background_policy == "paused":
        now = _utcnow()
        scheduling = dict(resident.scheduling_state or {})
        scheduling.update(
            {
                "shadow_background_suppressed_at": now.isoformat(),
                "shadow_background_suppressed_trigger": trigger_kind,
            }
        )
        resident.scheduling_state = scheduling
        session.add(resident)
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
    _repair_pointer_state(resident, tasks)
    active = _promote_successor(resident, tasks)

    if active is None:
        active = await _create_turn_task(
            session,
            resident,
            lane=lane,
            trigger_kind=trigger_kind,
            message_sequence=message_sequence,
            project_change_cursor=project_change_cursor,
            input_frozen=True,
        )
        resident.active_turn_task_id = active.id
        resident.active_turn_lane = lane
        state: Literal["queued", "coalesced"] = "queued"
        result_task = active
        successor_id = None
    else:
        attr = _lane_successor_attr(lane)
        successor = tasks.get(getattr(resident, attr))
        if not _is_mutable_successor_task(successor, project_id=project_id):
            successor = await _create_turn_task(
                session,
                resident,
                lane=lane,
                trigger_kind=trigger_kind,
                message_sequence=message_sequence,
                project_change_cursor=project_change_cursor,
                input_frozen=False,
            )
            setattr(resident, attr, successor.id)
        elif _is_mutable_successor_task(successor, project_id=project_id):
            _coalesce_successor_payload(
                successor,
                message_sequence=message_sequence,
                project_change_cursor=project_change_cursor,
            )
            session.add(successor)
        else:
            raise RuntimeError("Resident successor is claimed or already frozen")
        state = "coalesced"
        result_task = successor
        successor_id = successor.id

        if resident.active_turn_lane == "background" and lane == "interactive":
            scheduling = dict(resident.scheduling_state or {})
            scheduling.update(
                {
                    "preempt_requested_at": _utcnow().isoformat(),
                    "preempt_requested_generation": int(resident.authority_generation),
                    "preempt_for_task_id": str(successor.id),
                }
            )
            resident.scheduling_state = scheduling

    now = _utcnow()
    resident.last_wakeup_at = now
    scheduling = dict(resident.scheduling_state or {})
    scheduling.update(
        {
            "shadow_last_enqueue_at": now.isoformat(),
            "shadow_last_lane": lane,
            "shadow_last_state": state,
            "shadow_active_task_id": str(resident.active_turn_task_id),
        }
    )
    resident.scheduling_state = scheduling
    session.add(resident)
    return ResidentTurnWake(
        task_id=result_task.id,
        state=state,
        lane=lane,
        active_task_id=resident.active_turn_task_id,
        successor_task_id=successor_id,
    )


def __getattr__(name: str):
    """Preserve the pre-split projection imports without a circular dependency."""

    if name in {"MAX_MANIFEST_BYTES", "sweep_shadow_resident_turns"}:
        from app import strategic_resident_shadow

        return getattr(strategic_resident_shadow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "INPUT_FROZEN_CONTEXT_KEY",
    "MUTATION_AUTHORIZED_CONTEXT_KEY",
    "SHADOW_CONTEXT_KEY",
    "STRATEGIC_RESIDENT_TURN_TASK_TYPE",
    "ResidentTurnWake",
    "ShadowResidentOwnershipError",
    "coalesce_shadow_resident_turn",
    "initialize_strategic_resident",
    "lock_shadow_resident_turn_state",
]
