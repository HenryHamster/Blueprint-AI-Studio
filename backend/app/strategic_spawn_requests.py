"""Durable admission and dispatch for tasks requested by Strategic.

The caller holds the project lock. That makes idempotency, burst admission,
task creation, and the request state transition one transaction instead of a
read-then-create race. Ordinary work dispatches immediately; only the existing
autonomous spawn backstops or project-wide abuse circuit breaker defer accepted
intent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.depends_on_provenance import remember_structural_depends_on
from app.models import (
    DiscoveryCycle,
    Project,
    StrategicSpawnRequest,
    TASK_ORIGINATION_STRATEGIST,
    Task,
    TaskStatus,
    _utcnow,
)
from app.strategic_watches import register_task_watch
from app.task_locking import project_lock

DEFAULT_BURST_CAP = 100
DEFAULT_BURST_WINDOW_MINUTES = 60
MAX_DRAIN_PER_PASS = 10
MAX_PROJECTS_PER_SWEEP = 20
BUDGET_RECHECK_MINUTES = 5
PROJECT_FAIRNESS_YIELD_SECONDS = 1
ACTIVE_STRATEGIC_WORK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)

logger = logging.getLogger("blueprint")


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


def stable_work_specification_hash(specification: dict[str, Any]) -> str:
    """Hash executable work while excluding Resident-generation bookkeeping.

    A new Resident task/session must be able to recognize work already active
    under the same direction.  Watch presentation and the originating
    Strategic task id do not change that work; payload, acceptance contract,
    priority and the remaining context do.
    """

    task_spec = dict(specification.get("task") or {})
    raw_context = task_spec.get("context")
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    context.pop("parent_strategic_task_id", None)
    task_spec["context"] = context
    task_spec.pop("watch", None)
    task_spec.pop("watch_purpose", None)
    return stable_specification_hash({"task": task_spec})


def strategic_work_idempotency_key(
    *,
    project_id: uuid.UUID,
    direction_id: str | None,
    request_key: str,
) -> str:
    """Return the global active-Task identity for one logical work unit."""

    identity = json.dumps(
        [str(project_id), str(direction_id or "-"), request_key],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"strategic-direct:{hashlib.sha256(identity).hexdigest()}"


@dataclass(frozen=True)
class SpawnSubmission:
    request: StrategicSpawnRequest
    task: Task | None
    replayed: bool
    linked_debt_id: str | None = None


def _task_from_specification(
    *,
    project_id: uuid.UUID,
    specification: dict[str, Any],
    discovery_cycle_id: uuid.UUID | None = None,
    active_task_idempotency_key: str | None = None,
    work_specification_hash: str | None = None,
) -> Task:
    task_spec = specification.get("task")
    if not isinstance(task_spec, dict):
        raise ValueError("spawn request is missing its normalized task specification")
    payloads = task_spec.get("payloads")
    if not isinstance(payloads, list) or not payloads:
        raise ValueError("spawn request task specification requires payloads")
    context = task_spec.get("context")
    context = {} if not isinstance(context, dict) else dict(context)
    if active_task_idempotency_key:
        context["_strategic_work_key"] = active_task_idempotency_key
    if work_specification_hash:
        context["_strategic_work_specification_hash"] = work_specification_hash
    raw_priority = task_spec.get("priority")
    # Declared intra-batch ordering travels with the normalized specification,
    # so EVERY creation path applies it — the immediate (200) dispatch and the
    # deferred drain alike. Setting it here (rather than patching the row after
    # creation) also means the edge and the QUEUED row become visible to the
    # orchestrator in a single transaction, so the successor can never be
    # claimed in the window before its prerequisites are attached.
    depends_on_ids = [
        str(dep).strip()
        for dep in (task_spec.get("depends_on") or [])
        if str(dep).strip()
    ]
    deduped_depends_on: list[str] = []
    for dep in depends_on_ids:
        if dep not in deduped_depends_on:
            deduped_depends_on.append(dep)
    task = Task(
        project_id=project_id,
        discovery_cycle_id=discovery_cycle_id,
        title=str(task_spec.get("title") or "Strategic task")[:200],
        description=str(task_spec.get("description") or "")[:500],
        payloads=payloads,
        context=context,
        status=TaskStatus.QUEUED,
        priority=int(raw_priority if raw_priority is not None else 50),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        created_by="STRATEGIC_AGENT",
        origination=TASK_ORIGINATION_STRATEGIST,
        idempotency_key=active_task_idempotency_key,
        criterion_ids=list(task_spec.get("criterion_ids") or []),
        depends_on=deduped_depends_on,
        **(
            {"requires_approval": bool(task_spec["requires_approval"])}
            if task_spec.get("requires_approval") is not None
            else {}
        ),
    )
    if deduped_depends_on:
        # Structural provenance keeps the edge from being mistaken for a legacy
        # wave-serialization dep and swept by dependency-cleanup paths.
        remember_structural_depends_on(task, deduped_depends_on)
    return task


async def _dispatch_request(
    session,
    *,
    request: StrategicSpawnRequest,
) -> tuple[Task, str | None]:
    if request.task_id is not None:
        existing = await session.get(Task, request.task_id)
        if existing is not None:
            return existing, await _link_request_debt(
                session,
                request=request,
                task=existing,
            )
    if request.state not in {"accepted", "deferred"}:
        raise ValueError(
            f"strategic spawn request is no longer dispatchable ({request.state})"
        )
    task = _task_from_specification(
        project_id=request.project_id,
        specification=dict(request.specification or {}),
        discovery_cycle_id=request.discovery_cycle_id,
        active_task_idempotency_key=str(
            (request.policy_snapshot or {}).get("active_task_idempotency_key") or ""
        )
        or None,
        work_specification_hash=str(
            (request.policy_snapshot or {}).get("work_specification_hash") or ""
        )
        or None,
    )
    session.add(task)
    await session.flush()
    if (
        request.discovery_cycle_id is not None
        and request.task_type in {"research", "web_research"}
    ):
        cycle = await session.get(DiscoveryCycle, request.discovery_cycle_id)
        if cycle is not None and cycle.source_research_task_id is None:
            cycle.source_research_task_id = task.id
            session.add(cycle)

    request.task_id = task.id
    request.state = "dispatched"
    request.admission_reason = "immediate"
    request.not_before = None
    request.dispatched_at = _utcnow()
    session.add(request)

    await _register_request_watch(session, request=request, task=task)

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
    return task, await _link_request_debt(
        session,
        request=request,
        task=task,
    )


async def _register_request_watch(
    session,
    *,
    request: StrategicSpawnRequest,
    task: Task,
) -> None:
    task_spec = dict((request.specification or {}).get("task") or {})
    if not bool(task_spec.get("watch", True)) or request.parent_task_id is None:
        return
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


async def _active_task_for_work_key(session, work_key: str) -> Task | None:
    return (
        await session.execute(
            select(Task).where(
                Task.idempotency_key == work_key,
                Task.status.in_(ACTIVE_STRATEGIC_WORK_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def _budget_guard_for_request(
    session,
    *,
    task_type: str,
    specification: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply the existing autonomous backstop to a direct Strategic root."""

    from app.autonomous_spawn_budget import (
        autonomous_spawn_budget_config_from_runtime,
        classify_autonomous_spawn_budget_category,
    )
    from app.shared_state import runtime_config
    from app.strategic_spawning import _strategic_spawn_budget_guard

    task_spec = dict(specification.get("task") or {})
    payloads = task_spec.get("payloads") or []
    first_payload = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
    context = task_spec.get("context")
    if not isinstance(context, dict):
        context = {}
    try:
        config = autonomous_spawn_budget_config_from_runtime(runtime_config)
    except ValueError:
        # Keep classification available so the canonical guard can apply its
        # own established malformed-config fail posture.
        config = None
    category = classify_autonomous_spawn_budget_category(
        task_type=task_type,
        config=config,
        payload=first_payload,
        context=context,
    )
    return await _strategic_spawn_budget_guard(session, category)


def _defer_for_budget(
    request: StrategicSpawnRequest,
    *,
    guard: dict[str, Any],
    now,
) -> None:
    request.state = "deferred"
    request.admission_reason = str(guard.get("guard") or "spawn_budget")[:255]
    request.not_before = now + timedelta(minutes=BUDGET_RECHECK_MINUTES)
    request.policy_snapshot = {
        **(request.policy_snapshot or {}),
        "budget_guard": guard,
        "budget_recheck_minutes": BUDGET_RECHECK_MINUTES,
    }


async def _link_request_debt(
    session,
    *,
    request: StrategicSpawnRequest,
    task: Task,
) -> str | None:
    """Best-effort, idempotently link the eventual task to requested debt."""

    debt_id = (request.specification or {}).get("debt_id")
    if not debt_id:
        return None
    from app.completion_debt import link_task_to_debt

    try:
        await link_task_to_debt(
            session,
            debt_id,
            project_id=request.project_id,
            task_id=task.id,
            note="spawned by strategist",
        )
        return str(debt_id)
    except Exception:  # noqa: BLE001 - linkage must never lose accepted work
        logger.warning(
            "[STRATEGIC SPAWN] debt linkage failed (request=%s debt=%s task=%s)",
            request.id,
            debt_id,
            task.id,
            exc_info=True,
        )
        return None


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


async def _advance_due_requests_until_burst_room(
    session,
    *,
    project_id: uuid.UUID,
    since,
    window_minutes: int,
    now,
) -> None:
    """Move an ineligible due queue to its first real dispatch opportunity."""

    next_time = await _next_admission_time(
        session,
        project_id=project_id,
        since=since,
        window_minutes=window_minutes,
    )
    # The oldest dispatch can sit exactly on the inclusive window boundary.
    # Keep it out of the next sweep rather than repeatedly spending a global
    # project candidate on a row which still has no room.
    next_time = max(next_time, now + timedelta(seconds=1))
    await session.execute(
        update(StrategicSpawnRequest)
        .where(
            StrategicSpawnRequest.project_id == project_id,
            StrategicSpawnRequest.state == "deferred",
            StrategicSpawnRequest.not_before <= now,
        )
        .values(not_before=next_time, updated_at=now)
        .execution_options(synchronize_session="fetch")
    )


async def submit_strategic_spawn(
    session,
    *,
    project_id: uuid.UUID,
    parent_task_id: uuid.UUID,
    task_type: str,
    specification: dict[str, Any],
    idempotency_key: str | None,
    direction_id: str | None,
    discovery_cycle_id: uuid.UUID | None = None,
    burst_cap: int = DEFAULT_BURST_CAP,
    burst_window_minutes: int = DEFAULT_BURST_WINDOW_MINUTES,
) -> SpawnSubmission:
    """Persist one intent and immediately dispatch unless the abuse guard trips."""

    specification_hash = stable_specification_hash(specification)
    work_specification_hash = stable_work_specification_hash(specification)
    normalized_key = (
        str(idempotency_key or "").strip()[:128]
        or default_idempotency_key(work_specification_hash)
    )
    active_task_idempotency_key = strategic_work_idempotency_key(
        project_id=project_id,
        direction_id=direction_id,
        request_key=normalized_key,
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
        if existing.state in {"cancelled", "rejected"}:
            raise ValueError(
                f"spawn request is already terminal ({existing.state}); "
                "use a new idempotency key for new work"
            )
        existing_task = (
            await session.get(Task, existing.task_id)
            if existing.task_id is not None
            else None
        )
        linked_debt_id = (
            await _link_request_debt(
                session,
                request=existing,
                task=existing_task,
            )
            if existing_task is not None
            else None
        )
        if linked_debt_id is not None:
            await session.commit()
        return SpawnSubmission(existing, existing_task, True, linked_debt_id)

    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    if project.paused:
        raise ValueError("project is paused; strategic spawning is disabled")

    # The request row is intentionally parent-scoped for exact API replay, but
    # executable work is scoped to project + direction + logical request key.
    # That keeps a replacement Resident generation from duplicating work which
    # is still queued/running/parked, while the partial Task index releases the
    # key after terminal completion so a later retry remains possible.
    active_task = await _active_task_for_work_key(
        session, active_task_idempotency_key
    )
    if active_task is not None:
        active_work_hash = str(
            (active_task.context or {}).get("_strategic_work_specification_hash")
            or ""
        )
        if active_work_hash != work_specification_hash:
            raise ValueError(
                "idempotency_key already identifies different active work in "
                "this direction"
            )
        request = StrategicSpawnRequest(
            project_id=project_id,
            parent_task_id=parent_task_id,
            idempotency_key=normalized_key,
            task_type=task_type,
            specification=specification,
            specification_hash=specification_hash,
            direction_id=direction_id,
            discovery_cycle_id=active_task.discovery_cycle_id,
            state="dispatched",
            admission_reason="active_work_deduplicated",
            policy_snapshot={
                "kind": "active_work_deduplication",
                "active_task_idempotency_key": active_task_idempotency_key,
                "work_specification_hash": work_specification_hash,
            },
            task_id=active_task.id,
        )
        session.add(request)
        await session.flush()
        await _register_request_watch(session, request=request, task=active_task)
        linked_debt_id = await _link_request_debt(
            session,
            request=request,
            task=active_task,
        )
        await session.commit()
        return SpawnSubmission(request, active_task, True, linked_debt_id)

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
            "active_task_idempotency_key": active_task_idempotency_key,
            "work_specification_hash": work_specification_hash,
        },
    )
    session.add(request)
    await session.flush()
    from app.discovery_admission import resolve_discovery_cycle_lineage

    request.discovery_cycle_id = discovery_cycle_id
    if request.discovery_cycle_id is None:
        request.discovery_cycle_id = await resolve_discovery_cycle_lineage(
            session,
            parent_task_id=parent_task_id,
        )
    budget_guard = await _budget_guard_for_request(
        session,
        task_type=task_type,
        specification=specification,
    )
    if budget_guard is not None:
        _defer_for_budget(request, guard=budget_guard, now=now)
        session.add(request)
        await session.commit()
        return SpawnSubmission(request, None, False)

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

    child, linked_debt_id = await _dispatch_request(session, request=request)
    await session.commit()
    return SpawnSubmission(request, child, False, linked_debt_id)


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

    project = await session.get(Project, project_id)
    if project is None or project.paused:
        return []

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
        await _advance_due_requests_until_burst_room(
            session,
            project_id=project_id,
            since=since,
            window_minutes=window,
            now=now,
        )
        return []
    row_limit = max(1, min(int(limit), room, MAX_DRAIN_PER_PASS))
    candidates = (
        (
            await session.execute(
                select(StrategicSpawnRequest)
                .where(
                    StrategicSpawnRequest.project_id == project_id,
                    StrategicSpawnRequest.state == "deferred",
                    StrategicSpawnRequest.not_before <= now,
                )
                .order_by(
                    StrategicSpawnRequest.created_at.asc(),
                    StrategicSpawnRequest.id.asc(),
                )
                .limit(row_limit + 1)
            )
        )
        .scalars()
        .all()
    )
    rows = candidates[:row_limit]
    has_more_due = len(candidates) > row_limit
    drained: list[StrategicSpawnRequest] = []
    for request in rows:
        budget_guard = await _budget_guard_for_request(
            session,
            task_type=request.task_type,
            specification=dict(request.specification or {}),
        )
        if budget_guard is not None:
            _defer_for_budget(request, guard=budget_guard, now=now)
            session.add(request)
            continue
        await _dispatch_request(session, request=request)
        drained.append(request)
    if has_more_due:
        if len(drained) >= room:
            # This project filled its last burst slot during the current pass.
            await _advance_due_requests_until_burst_room(
                session,
                project_id=project_id,
                since=since,
                window_minutes=window,
                now=now,
            )
        else:
            # The queue hit the bounded per-project pass before its burst
            # window filled. Move only the untouched due tail behind every
            # project already waiting at this sweep boundary. ``not_before`` is
            # durable, so max_projects=1 remains fair across leader restarts
            # without adding a cursor/config subsystem.
            await session.execute(
                update(StrategicSpawnRequest)
                .where(
                    StrategicSpawnRequest.project_id == project_id,
                    StrategicSpawnRequest.state == "deferred",
                    StrategicSpawnRequest.not_before <= now,
                )
                .values(
                    not_before=now
                    + timedelta(seconds=PROJECT_FAIRNESS_YIELD_SECONDS),
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
    return drained


async def drain_due_strategic_spawn_requests(
    session,
    *,
    burst_cap: int = DEFAULT_BURST_CAP,
    burst_window_minutes: int = DEFAULT_BURST_WINDOW_MINUTES,
    max_projects: int = MAX_PROJECTS_PER_SWEEP,
) -> list[StrategicSpawnRequest]:
    """Autonomously drain due project queues under their mutation locks.

    This is the bounded orchestrator sweep entrypoint. It discovers due
    projects independently of Resident heartbeats, then reuses the same
    project-locked FIFO drainer as the heartbeat fast path. The caller commits.
    """

    now = _utcnow()
    oldest_due_at = func.min(StrategicSpawnRequest.not_before).label("oldest_due_at")
    oldest_created_at = func.min(StrategicSpawnRequest.created_at).label(
        "oldest_created_at"
    )
    project_ids = [
        row[0]
        for row in (
            await session.execute(
                select(
                    StrategicSpawnRequest.project_id,
                    oldest_due_at,
                    oldest_created_at,
                )
                .join(Project, Project.id == StrategicSpawnRequest.project_id)
                .where(
                    StrategicSpawnRequest.state == "deferred",
                    StrategicSpawnRequest.not_before <= now,
                    Project.paused.is_(False),
                )
                .group_by(StrategicSpawnRequest.project_id)
                .order_by(
                    oldest_due_at.asc(),
                    oldest_created_at.asc(),
                    StrategicSpawnRequest.project_id.asc(),
                )
                .limit(max(1, min(int(max_projects), MAX_PROJECTS_PER_SWEEP)))
            )
        ).all()
    ]
    drained: list[StrategicSpawnRequest] = []
    for project_id in project_ids:
        async with project_lock(
            session,
            project_id,
            skip_locked=True,
            load_columns=[Project.id, Project.paused],
        ) as locked_project:
            if locked_project is None or locked_project.paused:
                continue
            drained.extend(
                await drain_deferred_spawn_requests(
                    session,
                    project_id=project_id,
                    burst_cap=burst_cap,
                    burst_window_minutes=burst_window_minutes,
                )
            )
    return drained


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
