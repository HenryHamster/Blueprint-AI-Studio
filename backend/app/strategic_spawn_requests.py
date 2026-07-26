"""Durable admission and dispatch for tasks requested by Strategic.

The caller holds the project lock. That makes idempotency, burst admission,
task creation, and the request state transition one transaction instead of a
read-then-create race. Ordinary work dispatches immediately; only the
project-wide abuse circuit breaker defers accepted intent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import StrategicSpawnRequest, Task, TaskStatus, _utcnow
from app.strategic_watches import register_task_watch

DEFAULT_BURST_CAP = 100
DEFAULT_BURST_WINDOW_MINUTES = 60
MAX_DRAIN_PER_PASS = 10


def stable_specification_hash(specification: dict[str, Any]) -> str:
    encoded = json.dumps(
        specification,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_idempotency_key(specification_hash: str) -> str:
    return f"spec:{specification_hash}"


@dataclass(frozen=True)
class SpawnSubmission:
    request: StrategicSpawnRequest
    task: Task | None
    replayed: bool


def _task_from_specification(
    *,
    project_id: uuid.UUID,
    specification: dict[str, Any],
) -> Task:
    task_spec = specification.get("task")
    if not isinstance(task_spec, dict):
        raise ValueError("spawn request is missing its normalized task specification")
    payloads = task_spec.get("payloads")
    if not isinstance(payloads, list) or not payloads:
        raise ValueError("spawn request task specification requires payloads")
    context = task_spec.get("context")
    if not isinstance(context, dict):
        context = {}
    return Task(
        project_id=project_id,
        title=str(task_spec.get("title") or "Strategic task")[:200],
        description=str(task_spec.get("description") or "")[:500],
        payloads=payloads,
        context=context,
        status=TaskStatus.QUEUED,
        priority=int(task_spec.get("priority") or 100),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        created_by="STRATEGIC_AGENT",
        criterion_ids=list(task_spec.get("criterion_ids") or []),
        **(
            {"requires_approval": bool(task_spec["requires_approval"])}
            if task_spec.get("requires_approval") is not None
            else {}
        ),
    )


async def _dispatch_request(
    session,
    *,
    request: StrategicSpawnRequest,
) -> Task:
    if request.task_id is not None:
        existing = await session.get(Task, request.task_id)
        if existing is not None:
            return existing

    task = _task_from_specification(
        project_id=request.project_id,
        specification=dict(request.specification or {}),
    )
    session.add(task)
    await session.flush()

    request.task_id = task.id
    request.state = "dispatched"
    request.admission_reason = "immediate"
    request.not_before = None
    request.dispatched_at = _utcnow()
    session.add(request)

    task_spec = dict((request.specification or {}).get("task") or {})
    if bool(task_spec.get("watch", True)) and request.parent_task_id is not None:
        await register_task_watch(
            session,
            project_id=request.project_id,
            created_by_task_id=request.parent_task_id,
            target_task_id=task.id,
            purpose=str(
                task_spec.get("watch_purpose")
                or f"Report when Strategic child {task.title} reaches terminal state"
            ),
        )

    if request.parent_task_id is not None:
        parent = await session.get(Task, request.parent_task_id)
        if parent is not None:
            context = dict(parent.context or {})
            context["_strategic_tasks_spawned"] = (
                int(context.get("_strategic_tasks_spawned") or 0) + 1
            )
            parent.context = context
            flag_modified(parent, "context")
            session.add(parent)
    return task


async def _recent_dispatch_count(
    session,
    *,
    project_id: uuid.UUID,
    since,
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(StrategicSpawnRequest)
                .where(
                    StrategicSpawnRequest.project_id == project_id,
                    StrategicSpawnRequest.state == "dispatched",
                    StrategicSpawnRequest.dispatched_at >= since,
                )
            )
        ).scalar()
        or 0
    )


async def _next_admission_time(
    session,
    *,
    project_id: uuid.UUID,
    since,
    window_minutes: int,
):
    oldest = (
        await session.execute(
            select(StrategicSpawnRequest.dispatched_at)
            .where(
                StrategicSpawnRequest.project_id == project_id,
                StrategicSpawnRequest.state == "dispatched",
                StrategicSpawnRequest.dispatched_at >= since,
            )
            .order_by(StrategicSpawnRequest.dispatched_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (oldest or _utcnow()) + timedelta(minutes=window_minutes)


async def submit_strategic_spawn(
    session,
    *,
    project_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    task_type: str,
    specification: dict[str, Any],
    idempotency_key: str | None,
    direction_id: str | None,
    burst_cap: int = DEFAULT_BURST_CAP,
    burst_window_minutes: int = DEFAULT_BURST_WINDOW_MINUTES,
) -> SpawnSubmission:
    """Persist one intent and immediately dispatch unless the abuse guard trips."""

    specification_hash = stable_specification_hash(specification)
    normalized_key = (
        str(idempotency_key or "").strip()[:128]
        or default_idempotency_key(specification_hash)
    )
    existing = (
        await session.execute(
            select(StrategicSpawnRequest).where(
                StrategicSpawnRequest.parent_task_id == parent_task_id,
                StrategicSpawnRequest.idempotency_key == normalized_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.specification_hash != specification_hash:
            raise ValueError(
                "idempotency_key was already used for a different spawn specification"
            )
        existing_task = (
            await session.get(Task, existing.task_id)
            if existing.task_id is not None
            else None
        )
        return SpawnSubmission(existing, existing_task, True)

    now = _utcnow()
    cap = max(1, int(burst_cap))
    window = max(1, int(burst_window_minutes))
    request = StrategicSpawnRequest(
        project_id=project_id,
        parent_task_id=parent_task_id,
        idempotency_key=normalized_key,
        task_type=task_type,
        specification=specification,
        specification_hash=specification_hash,
        direction_id=direction_id,
        state="accepted",
        policy_snapshot={
            "kind": "abuse_circuit_breaker",
            "burst_cap": cap,
            "window_minutes": window,
        },
    )
    session.add(request)
    await session.flush()

    since = now - timedelta(minutes=window)
    recent_count = await _recent_dispatch_count(
        session,
        project_id=project_id,
        since=since,
    )
    if recent_count >= cap:
        request.state = "deferred"
        request.admission_reason = "project_spawn_burst"
        request.not_before = await _next_admission_time(
            session,
            project_id=project_id,
            since=since,
            window_minutes=window,
        )
        session.add(request)
        await session.commit()
        return SpawnSubmission(request, None, False)

    child = await _dispatch_request(session, request=request)
    await session.commit()
    return SpawnSubmission(request, child, False)


async def drain_deferred_spawn_requests(
    session,
    *,
    project_id: uuid.UUID,
    burst_cap: int = DEFAULT_BURST_CAP,
    burst_window_minutes: int = DEFAULT_BURST_WINDOW_MINUTES,
    limit: int = MAX_DRAIN_PER_PASS,
) -> list[StrategicSpawnRequest]:
    """Drain due requests FIFO while the abuse window has room.

    The caller must hold the project lock and commits after this function.
    """

    now = _utcnow()
    cap = max(1, int(burst_cap))
    window = max(1, int(burst_window_minutes))
    since = now - timedelta(minutes=window)
    room = cap - await _recent_dispatch_count(
        session,
        project_id=project_id,
        since=since,
    )
    if room <= 0:
        return []
    rows = (
        (
            await session.execute(
                select(StrategicSpawnRequest)
                .where(
                    StrategicSpawnRequest.project_id == project_id,
                    StrategicSpawnRequest.state == "deferred",
                    StrategicSpawnRequest.not_before <= now,
                )
                .order_by(StrategicSpawnRequest.created_at.asc())
                .limit(max(1, min(int(limit), room, MAX_DRAIN_PER_PASS)))
            )
        )
        .scalars()
        .all()
    )
    for request in rows:
        await _dispatch_request(session, request=request)
    return rows


async def cancel_deferred_spawn_request(
    session,
    *,
    project_id: uuid.UUID,
    request_id: uuid.UUID,
) -> StrategicSpawnRequest:
    request = await session.get(StrategicSpawnRequest, request_id)
    if request is None or request.project_id != project_id:
        raise ValueError("spawn request not found in project")
    if request.state == "deferred":
        request.state = "cancelled"
        request.admission_reason = "cancelled_by_strategic"
        request.not_before = None
        session.add(request)
    elif request.state not in {"cancelled", "rejected"}:
        raise ValueError("only a deferred spawn request can be cancelled")
    return request
