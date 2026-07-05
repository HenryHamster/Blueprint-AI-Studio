"""Helpers for extracting direction references from task-shaped JSON."""

from __future__ import annotations

from typing import Any

from app.models import Task


_DIRECTION_ID_KEYS = (
    "direction_id",
    "directionId",
    "source_direction_id",
    "sourceDirectionId",
    "target_direction_id",
    "targetDirectionId",
    "current_direction_id",
    "currentDirectionId",
)

_PRIMARY_CONTEXT_DIRECTION_KEYS = (
    "direction_id",
    "directionId",
    "current_direction_id",
    "currentDirectionId",
)

_NESTED_DIRECTION_CONTAINERS = (
    "plan_output",
    "findings",
    "research_findings",
    "subtasks",
    "tasks",
    "items",
    "candidate_tasks",
    "recommended_tasks",
)

_DIRECT_PAYLOAD_DIRECTION_KEYS = (
    "direction_id",
    "directionId",
    "current_direction_id",
    "currentDirectionId",
    "source_direction_id",
    "sourceDirectionId",
    "target_direction_id",
    "targetDirectionId",
)


def _direction_id_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = str(value).strip()
    return cleaned or None


def _add_direction_id(ids: list[str], seen: set[str], value: Any) -> None:
    direction_id = _direction_id_value(value)
    if direction_id and direction_id not in seen:
        seen.add(direction_id)
        ids.append(direction_id)


def _collect_direction_ids(value: Any, ids: list[str], seen: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_direction_ids(item, ids, seen)
        return

    if not isinstance(value, dict):
        return

    for key in _DIRECTION_ID_KEYS:
        _add_direction_id(ids, seen, value.get(key))

    directions = value.get("directions")
    if isinstance(directions, list):
        for direction in directions:
            if isinstance(direction, dict):
                _add_direction_id(ids, seen, direction.get("id"))
                for key in _DIRECTION_ID_KEYS:
                    _add_direction_id(ids, seen, direction.get(key))
            else:
                _add_direction_id(ids, seen, direction)

    for key in _NESTED_DIRECTION_CONTAINERS:
        _collect_direction_ids(value.get(key), ids, seen)


def _direct_direction_ids(
    value: Any,
    keys: tuple[str, ...] = _DIRECTION_ID_KEYS,
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    if not isinstance(value, dict):
        return ids
    for key in keys:
        _add_direction_id(ids, seen, value.get(key))
    return ids


def _payload_direction_ids(payloads: Any, *, include_related: bool) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    if not isinstance(payloads, list):
        return ids
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in _DIRECT_PAYLOAD_DIRECTION_KEYS:
            _add_direction_id(ids, seen, payload.get(key))
        directions = payload.get("directions")
        if directions and (include_related or not ids):
            if isinstance(directions, list):
                for direction in directions:
                    if isinstance(direction, dict):
                        _add_direction_id(ids, seen, direction.get("id"))
                    _collect_direction_ids(direction, ids, seen)
            else:
                _collect_direction_ids(directions, ids, seen)
        if include_related:
            for key in _NESTED_DIRECTION_CONTAINERS:
                _collect_direction_ids(payload.get(key), ids, seen)
    return ids


def task_related_direction_ids(task: Task) -> list[str]:
    """Return stable, de-duplicated direction ids associated with a task."""

    ids: list[str] = []
    seen: set[str] = set()
    _collect_direction_ids(task.context, ids, seen)
    _collect_direction_ids(task.payloads, ids, seen)
    _collect_direction_ids(task.plan_output, ids, seen)
    _collect_direction_ids(getattr(task, "research_findings", None), ids, seen)
    return ids


def task_primary_direction_ids(task: Task) -> list[str]:
    """Return direction ids that describe task ownership, not loose references."""

    context_ids = _direct_direction_ids(task.context, _PRIMARY_CONTEXT_DIRECTION_KEYS)
    if context_ids:
        return context_ids

    payload_ids = _payload_direction_ids(task.payloads, include_related=False)
    if payload_ids:
        return payload_ids

    plan_ids = _direct_direction_ids(task.plan_output)
    if plan_ids:
        return plan_ids

    ids: list[str] = []
    seen: set[str] = set()
    if isinstance(task.plan_output, dict):
        for key in ("findings", "research_findings", "subtasks", "tasks"):
            _collect_direction_ids(task.plan_output.get(key), ids, seen)
    _collect_direction_ids(getattr(task, "research_findings", None), ids, seen)
    return ids


def task_primary_direction_ids_shallow(task: Task) -> list[str]:
    """Like task_primary_direction_ids but skips plan_output (for cheap queries).

    Safe to call when plan_output is not loaded; uses only context and payloads.
    """
    context_ids = _direct_direction_ids(task.context, _PRIMARY_CONTEXT_DIRECTION_KEYS)
    if context_ids:
        return context_ids
    return _payload_direction_ids(task.payloads, include_related=False)


def first_task_direction_id(task: Task) -> str | None:
    ids = task_primary_direction_ids(task)
    return ids[0] if ids else None
