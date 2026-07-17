"""Exact, bounded project-scoped Task evidence for Resident capabilities."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.models import Task
from app.schemas import redact_command_log_text


TaskInspectionKind = Literal["task", "lineage", "logs", "failure", "provider"]
MAX_LOG_BYTES = 48_000
MAX_LOG_ENTRY_BYTES = 4_000
MAX_TEXT_CHARS = 4_000
MAX_TASK_LABEL_CHARS = 512
_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "client_secret",
    "credential",
    "api_key",
    "password",
    "private_key",
    "refresh_token",
    "access_token",
    "id_token",
    "secret",
    "token",
    "cookie",
)


class ResidentTaskEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TaskInspectionKind
    project_id: uuid.UUID
    source_id: str
    revision: str
    checksum: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    data: dict[str, Any]


class ResidentTaskNotFound(LookupError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _sensitive_key(value: Any) -> bool:
    normalized = "".join(character for character in str(value).casefold() if character.isalnum())
    return any(
        "".join(character for character in marker if character.isalnum()) in normalized
        for marker in _SENSITIVE_KEY_MARKERS
    )


def _bounded_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    remaining = budget if budget is not None else [500]
    if remaining[0] <= 0 or depth >= 6:
        return "[truncated]"
    remaining[0] -= 1
    if isinstance(value, str):
        return redact_command_log_text(value)[:MAX_TEXT_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value, key=str)[:50]:
            bounded_key = redact_command_log_text(str(key))[:160]
            result[bounded_key] = (
                "[redacted]"
                if _sensitive_key(key)
                else _bounded_value(value[key], depth=depth + 1, budget=remaining)
            )
            if remaining[0] <= 0:
                break
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_value(item, depth=depth + 1, budget=remaining)
            for item in value[:50]
            if remaining[0] > 0
        ]
    return str(_jsonable(value))[:MAX_TEXT_CHARS]


def _bounded_label(value: Any) -> Any:
    """Bound scalar task metadata that is repeated across lineage entries."""

    bounded = _bounded_value(value)
    if isinstance(bounded, str):
        return bounded[:MAX_TASK_LABEL_CHARS]
    return bounded


def _bounded_log_entry(value: Any) -> Any:
    bounded = _bounded_value(value)
    encoded = json.dumps(
        bounded, sort_keys=True, separators=(",", ":"), default=_jsonable
    ).encode()
    if len(encoded) <= MAX_LOG_ENTRY_BYTES:
        return bounded
    return {
        "truncated": True,
        "content_checksum": hashlib.sha256(encoded).hexdigest(),
        "preview": encoded[: MAX_LOG_ENTRY_BYTES - 256].decode(
            "utf-8", errors="ignore"
        ),
    }


def _bounded_status_log(values: Any, *, limit: int) -> tuple[list[Any], bool]:
    source_values = values if isinstance(values, list) else []
    source = source_values[-limit:]
    kept_reversed: list[Any] = []
    used = 2
    for raw in reversed(source):
        item = _bounded_log_entry(raw)
        item_size = len(json.dumps(item, default=_jsonable).encode()) + 1
        if kept_reversed and used + item_size > MAX_LOG_BYTES:
            break
        kept_reversed.append(item)
        used += item_size
    kept_reversed.reverse()
    return kept_reversed, (
        len(source_values) > len(source) or len(kept_reversed) < len(source)
    )


def _task_data(task: Task, kind: TaskInspectionKind, *, log_limit: int) -> dict[str, Any]:
    payload = task.first_payload
    common = {
        "task_id": str(task.id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "title": _bounded_label(task.title),
        "type": _bounded_label(payload.get("type")),
        "status": _jsonable(task.status),
        "version": int(task.version),
        "priority": int(task.priority),
        "attempt_count": int(task.attempt_count),
        "created_at": _jsonable(task.created_at),
        "updated_at": _jsonable(task.updated_at),
    }
    if kind == "task":
        depends_on = [str(item)[:160] for item in list(task.depends_on or [])[:50]]
        return {
            **common,
            "depends_on": depends_on,
            "depends_on_truncated": len(task.depends_on or []) > len(depends_on),
            "requires_approval": bool(task.requires_approval),
            "branch": _bounded_label(
                payload.get("branch") or (task.context or {}).get("branch")
            ),
            "pr_number": _bounded_label(
                payload.get("pr_number") or (task.context or {}).get("pr_number")
            ),
            "latest_status_message": _bounded_value(task.latest_status_message),
        }
    if kind == "logs":
        status_log, log_truncated = _bounded_status_log(
            task.status_log, limit=log_limit
        )
        return {
            **common,
            "latest_status_message": _bounded_value(task.latest_status_message),
            "status_log": status_log,
            "status_log_truncated": log_truncated,
        }
    if kind == "failure":
        context = task.context or {}
        return {
            **common,
            "latest_status_message": _bounded_value(task.latest_status_message),
            "blocker_kind": _bounded_label(task.blocker_kind),
            "blocked_reason": _bounded_value(task.blocked_reason),
            "park_reason": _bounded_value(task.park_reason),
            "error": _bounded_value(context.get("error") or context.get("failure_reason")),
            "error_fingerprint": _bounded_value(context.get("error_fingerprint") or task.previous_error_fingerprint),
            "failure_category": _bounded_value(context.get("failure_category")),
        }
    if kind == "provider":
        context = task.context or {}
        return {
            **common,
            "provider": _bounded_label(task.provider or context.get("llm_provider")),
            "model": _bounded_label(task.model or context.get("llm_model")),
            "account_id": _bounded_label(task.account_id or task.llm_account_id),
            "vm_instance_id": _bounded_label(task.vm_instance_id),
            "vm_machine_type": _bounded_label(task.vm_machine_type),
            "last_heartbeat": _jsonable(task.last_heartbeat),
        }
    return common


def _evidence(
    task: Task,
    kind: TaskInspectionKind,
    data: dict[str, Any],
    *,
    observed_at: datetime,
) -> ResidentTaskEvidence:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=_jsonable).encode()
    return ResidentTaskEvidence(
        kind=kind,
        project_id=task.project_id,
        source_id=str(task.id),
        revision=f"task:{task.id}:v{int(task.version)}:{_jsonable(task.updated_at)}",
        checksum=hashlib.sha256(encoded).hexdigest(),
        observed_at=observed_at,
        data=data,
    )


async def inspect_project_task(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    kind: Literal["task", "logs", "failure", "provider"] = "task",
    log_limit: int = 50,
    observed_at: datetime | None = None,
) -> ResidentTaskEvidence:
    """Inspect one exact task; the project predicate is the tenant boundary."""

    if not 1 <= log_limit <= 90:
        raise ValueError("log_limit must be between 1 and 90")
    task = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.project_id == project_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise ResidentTaskNotFound("task not found in project")
    now = observed_at or datetime.now(timezone.utc)
    return _evidence(task, kind, _task_data(task, kind, log_limit=log_limit), observed_at=now)


async def inspect_project_task_lineage(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    max_tasks: int = 50,
    observed_at: datetime | None = None,
) -> ResidentTaskEvidence:
    """Return ancestors plus descendants in stable order, bounded to one project."""

    if not 1 <= max_tasks <= 100:
        raise ValueError("max_tasks must be between 1 and 100")
    root = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.project_id == project_id)
        )
    ).scalar_one_or_none()
    if root is None:
        raise ResidentTaskNotFound("task not found in project")
    ancestor_task = aliased(Task)
    ancestors = select(
        Task.id.label("id"), Task.parent_task_id.label("parent_id"), literal(0).label("depth")
    ).where(Task.id == task_id, Task.project_id == project_id).cte(
        "resident_task_ancestors", recursive=True
    )
    ancestors = ancestors.union_all(
        select(
            ancestor_task.id,
            ancestor_task.parent_task_id,
            (ancestors.c.depth + 1).label("depth"),
        ).where(
            ancestor_task.id == ancestors.c.parent_id,
            ancestor_task.project_id == project_id,
            ancestors.c.depth < max_tasks,
        )
    )
    ancestor_rows = (
        await session.execute(
            select(Task, ancestors.c.depth)
            .join(ancestors, Task.id == ancestors.c.id)
            .where(ancestors.c.depth > 0)
            .order_by(ancestors.c.depth.asc())
            .limit(max_tasks + 1)
        )
    ).all()

    child_task = aliased(Task)
    descendants = select(
        Task.id.label("id"), literal(0).label("depth")
    ).where(Task.id == task_id, Task.project_id == project_id).cte(
        "resident_task_descendants", recursive=True
    )
    descendants = descendants.union_all(
        select(
            child_task.id,
            (descendants.c.depth + 1).label("depth"),
        ).where(
            child_task.parent_task_id == descendants.c.id,
            child_task.project_id == project_id,
            descendants.c.depth < max_tasks,
        )
    )
    descendant_rows = (
        await session.execute(
            select(Task, descendants.c.depth)
            .join(descendants, Task.id == descendants.c.id)
            .where(descendants.c.depth > 0)
            .order_by(descendants.c.depth.asc(), Task.created_at.asc(), Task.id.asc())
            .limit(max_tasks + 1)
        )
    ).all()

    ancestors_near_first = [row[0] for row in ancestor_rows]
    descendants_ordered = [row[0] for row in descendant_rows]
    candidates = [*reversed(ancestors_near_first), root, *descendants_ordered]
    by_id: dict[uuid.UUID, Task] = {}
    ordered_tasks: list[Task] = []
    for candidate in candidates:
        if candidate.id in by_id:
            continue
        by_id[candidate.id] = candidate
        ordered_tasks.append(candidate)
    truncated = len(ordered_tasks) > max_tasks
    ordered_tasks = ordered_tasks[:max_tasks]
    data = {
        "requested_task_id": str(task_id),
        "truncated": truncated,
        "tasks": [_task_data(item, "lineage", log_limit=1) for item in ordered_tasks],
    }
    return _evidence(
        root,
        "lineage",
        data,
        observed_at=observed_at or datetime.now(timezone.utc),
    )


__all__ = [
    "ResidentTaskEvidence",
    "ResidentTaskNotFound",
    "inspect_project_task",
    "inspect_project_task_lineage",
]
