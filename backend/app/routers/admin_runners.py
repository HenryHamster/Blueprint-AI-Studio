"""Admin endpoints for managing pull-based fleet runners.

A fleet runner is a process on operator hardware that polls the cloud
orchestrator for work (see ``app.routers.runner``). These endpoints let a
project owner enroll a runner (minting its one-time secret token), list the
project's runners, and revoke one. Only the SHA256 hash of a runner's token is
ever stored — the plaintext is returned exactly once, at registration.
"""

import logging
import uuid
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import create_runner_token, hash_runner_token, get_current_user
from app.database import get_session
from app.models import LocalRunner, Task, TaskStatus, _utcnow
from app.task_capabilities import STRATEGIC_TASK_TYPES, TaskType
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.workbench_state import clear_preferred_runner_references

logger = logging.getLogger("blueprint")

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-runners"],
    dependencies=[Depends(get_current_user)],
)


# Default capability shape for a freshly registered runner: handles every task
# type, no GPU, two concurrent tasks. Callers may override any subset.
_DEFAULT_CAPABILITIES: Dict[str, Any] = {
    "task_types": "all",
    "has_gpu": False,
    "max_concurrent": 2,
}

_RUNNER_LIVENESS_SECONDS = 300
_STRATEGIC_LOCAL_TASK_TYPES = sorted(STRATEGIC_TASK_TYPES)
_KNOWN_TASK_TYPES = {task_type.value for task_type in TaskType}

RunnerPreset = Literal["default", "strategic", "strategic_local"]


class RunnerRegisterRequest(BaseModel):
    hostname: str | None = None
    version: str | None = None
    capabilities: Dict[str, Any] | None = None
    preset: RunnerPreset | None = None


def _normalize_capabilities(
    raw: Dict[str, Any] | None,
    preset: RunnerPreset | None = None,
) -> Dict[str, Any]:
    """Merge a caller-supplied capabilities dict over the defaults."""
    caps = dict(_DEFAULT_CAPABILITIES)
    if preset in ("strategic", "strategic_local"):
        caps.update(
            {
                "task_types": list(_STRATEGIC_LOCAL_TASK_TYPES),
                "max_concurrent": 1,
                "runner_preset": "strategic_local",
                "workload": "strategic",
            }
        )
    if raw:
        caps.update(raw)
    if preset in ("strategic", "strategic_local"):
        caps["task_types"] = list(_STRATEGIC_LOCAL_TASK_TYPES)
        caps["max_concurrent"] = 1
        caps["runner_preset"] = "strategic_local"
        caps["workload"] = "strategic"
    caps["task_types"] = _normalize_task_types(caps.get("task_types", "all"))
    caps["max_concurrent"] = _normalize_max_concurrent(caps.get("max_concurrent"))
    if "has_gpu" in caps and not isinstance(caps["has_gpu"], bool):
        raise ValueError("capabilities.has_gpu must be a boolean")
    return caps


def _normalize_task_types(raw: Any) -> str | list[str]:
    if raw == "all":
        return "all"
    if not isinstance(raw, list) or not raw:
        raise ValueError("capabilities.task_types must be 'all' or a non-empty list")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ValueError("capabilities.task_types entries must be task type strings")
        if item not in _KNOWN_TASK_TYPES:
            raise ValueError(f"unknown task type for runner capability: {item}")
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def _normalize_max_concurrent(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("capabilities.max_concurrent must be a positive integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError("capabilities.max_concurrent must be a positive integer")
        value = int(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped.isdecimal():
            raise ValueError("capabilities.max_concurrent must be a positive integer")
        value = int(stripped)
    else:
        raise ValueError("capabilities.max_concurrent must be a positive integer")
    if value < 1 or value > 64:
        raise ValueError("capabilities.max_concurrent must be between 1 and 64")
    return value


def _runner_active_task_count(capabilities: Dict[str, Any] | None) -> int:
    active = (capabilities or {}).get("active_task_ids") or []
    return len(active) if isinstance(active, list) else 0


def _runner_active_compute_job_count(capabilities: Dict[str, Any] | None) -> int:
    active = (capabilities or {}).get("active_compute_job_ids") or []
    return len(active) if isinstance(active, list) else 0


def _runner_max_concurrent(capabilities: Dict[str, Any] | None) -> int | None:
    raw = (capabilities or {}).get("max_concurrent")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() and raw > 0 else None
    if isinstance(raw, str):
        try:
            parsed = int(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _runner_health(runner: LocalRunner) -> Dict[str, Any]:
    """Return the dispatch-relevant health shape used by the operator UI."""
    now = _utcnow()
    if runner.last_seen_at is None:
        seconds_since_last_seen = None
    else:
        seconds_since_last_seen = max(
            0, int((now - runner.last_seen_at).total_seconds())
        )
    active_task_count = _runner_active_task_count(runner.capabilities)
    active_compute_job_count = _runner_active_compute_job_count(runner.capabilities)
    active_work_count = active_task_count + active_compute_job_count
    max_concurrent = _runner_max_concurrent(runner.capabilities)
    available_slots = (
        max(0, max_concurrent - active_work_count)
        if max_concurrent is not None
        else None
    )
    is_live = (
        runner.status == "active"
        and seconds_since_last_seen is not None
        and seconds_since_last_seen < _RUNNER_LIVENESS_SECONDS
    )
    stale_reason = None
    if runner.status != "active":
        stale_reason = "inactive" if runner.status == "inactive" else runner.status
    elif seconds_since_last_seen is None:
        stale_reason = "never_seen"
    elif seconds_since_last_seen >= _RUNNER_LIVENESS_SECONDS:
        stale_reason = "heartbeat_stale"
    if is_live:
        device_status = "connected"
        device_status_reason = None
    elif runner.status != "active":
        device_status = "offline"
        device_status_reason = stale_reason
    else:
        device_status = "stale"
        device_status_reason = stale_reason
    return {
        "device_status": device_status,
        "device_status_reason": device_status_reason,
        "is_live": is_live,
        "seconds_since_last_seen": seconds_since_last_seen,
        "active_task_count": active_task_count,
        "active_compute_job_count": active_compute_job_count,
        "active_work_count": active_work_count,
        "max_concurrent": max_concurrent,
        "available_slots": available_slots,
        "stale_reason": stale_reason,
        "agent_available": (runner.capabilities or {}).get("agent_available"),
        "liveness_window_seconds": _RUNNER_LIVENESS_SECONDS,
    }


def _runner_view(runner: LocalRunner) -> Dict[str, Any]:
    return {
        "id": str(runner.id),
        "hostname": runner.hostname,
        "version": runner.version,
        "status": runner.status,
        "capabilities": runner.capabilities,
        "health": _runner_health(runner),
        "last_seen_at": (
            runner.last_seen_at.isoformat() if runner.last_seen_at else None
        ),
        "created_at": runner.created_at.isoformat() if runner.created_at else None,
    }


@router.post("/projects/{project_id}/runners/register")
async def register_runner(
    project_id: uuid.UUID,
    payload: RunnerRegisterRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Register a new fleet runner and return its one-time secret token.

    The plaintext token is included in the response exactly once and is never
    stored — only its SHA256 hash is persisted on the runner row.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    try:
        capabilities = _normalize_capabilities(payload.capabilities, payload.preset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    runner = LocalRunner(
        project_id=project_id,
        token_hash="",  # filled below once we have the runner id for the token
        hostname=payload.hostname,
        version=payload.version,
        capabilities=capabilities,
    )
    token = create_runner_token(runner.id)
    runner.token_hash = hash_runner_token(token)
    session.add(runner)
    await session.commit()
    await session.refresh(runner)

    logger.info(
        "[RUNNERS] Registered runner %s for project %s (hostname=%s)",
        runner.id,
        project_id,
        payload.hostname,
    )
    await emit_workbench_refetch(
        project_id,
        resource="local_runner",
        action="registered",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="runner",
        summary={
            "id": str(runner.id),
            "status": runner.status,
            "hostname": runner.hostname,
            "preset": (runner.capabilities or {}).get("runner_preset"),
        },
    )
    # The token is surfaced ONCE here and never again.
    return {"runner_id": str(runner.id), "token": token}


@router.get("/projects/{project_id}/runners")
async def list_runners(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List the project's runners (excluding deleted ones)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    result = await session.execute(
        select(LocalRunner)
        .where(
            LocalRunner.project_id == project_id,
            LocalRunner.status != "deleted",
        )
        .order_by(LocalRunner.created_at.desc())
    )
    return [_runner_view(r) for r in result.scalars().all()]


async def _release_unpulled_runner_pins(
    session: AsyncSession,
    runner_id: uuid.UUID,
) -> int:
    """Release tasks pinned to a runner before the runner pulled them."""
    pinned = (
        await session.execute(
            select(Task).where(
                Task.status == TaskStatus.PROVISIONING,
                Task.started_at.is_(None),
                Task.vm_instance_id == f"runner:{runner_id}",
            )
        )
    ).scalars().all()
    for task in pinned:
        task.status = TaskStatus.QUEUED
        task.vm_instance_id = None
        task.vm_provisioned_at = None
        new_ctx = dict(task.context or {})
        new_ctx.pop("runner_target_id", None)
        task.context = new_ctx
        session.add(task)
    return len(pinned)


@router.delete("/projects/{project_id}/runners/{runner_id}")
async def delete_runner(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Revoke a runner by marking it deleted (token can no longer authenticate)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    result = await session.execute(
        select(LocalRunner)
        .where(
            LocalRunner.id == runner_id,
            LocalRunner.project_id == project_id,
        )
        .with_for_update()
    )
    runner = result.scalars().first()
    if runner is None or runner.status == "deleted":
        raise HTTPException(status_code=404, detail="Runner not found")

    runner.status = "deleted"
    session.add(runner)
    cleared_runner_references = await clear_preferred_runner_references(
        session,
        project_id,
        runner_id,
    )
    released_pins = await _release_unpulled_runner_pins(session, runner_id)
    await session.commit()
    logger.info(
        "[RUNNERS] Deleted runner %s for project %s (released_unpulled_pins=%s cleared_refs=%s)",
        runner_id,
        project_id,
        released_pins,
        cleared_runner_references,
    )
    await emit_workbench_refetch(
        project_id,
        resource="local_runner",
        action="deleted",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="runner",
        summary={
            "id": str(runner_id),
            "status": "deleted",
            "released_unpulled_pins": released_pins,
        },
    )
    return {
        "runner_id": str(runner_id),
        "status": "deleted",
        "released_unpulled_pins": released_pins,
    }
