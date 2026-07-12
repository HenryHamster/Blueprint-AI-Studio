"""Admin endpoints for managing pull-based fleet runners.

A fleet runner is a process on operator hardware that polls the cloud
orchestrator for work (see ``app.routers.runner``). These endpoints let a
project owner enroll a runner (minting its one-time secret token), list the
project's runners, and revoke one. Only the SHA256 hash of a runner's token is
ever stored — the plaintext is returned exactly once, at registration.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import create_runner_token, hash_runner_token, get_current_user, require_admin
from app.database import get_session
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    RunnerHeartbeatSnapshot,
    Task,
    TASK_OUTCOME_TERMINAL_STATUSES,
    TaskStatus,
    _utcnow,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES, TaskType
from app.vm_cost_rates import AcceleratorRejection, get_compute_rate_per_second
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.runner_health_monitors import (
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
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
_SUCCESS_STALE_SECONDS = 1800  # 30 min
# Advisory low-disk flag (does not by itself degrade a runner).
_LOW_DISK_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
# Advisory low-memory flag (does not by itself degrade a runner).
_LOW_MEMORY_FREE_BYTES = 512 * 1024 * 1024  # 512 MiB
_STRATEGIC_LOCAL_TASK_TYPES = sorted(STRATEGIC_TASK_TYPES)
_KNOWN_TASK_TYPES = {task_type.value for task_type in TaskType}

RunnerPreset = Literal["default", "strategic", "strategic_local"]


class RunnerRegisterRequest(BaseModel):
    hostname: str | None = None
    version: str | None = None
    capabilities: Dict[str, Any] | None = None
    preset: RunnerPreset | None = None


class RunnerUpdateCapabilitiesRequest(BaseModel):
    """Request body for PATCH /projects/../runners/../capabilities.

    All fields are optional — only the provided fields are updated.
    """
    max_concurrent: int | None = None
    task_types: str | list[str] | None = None
    max_concurrent_compute: int | None = None


class RunnerStatusUpdateRequest(BaseModel):
    """Request body for PATCH /projects/../runners/../status."""
    status: Literal["active", "paused", "draining"]


# Valid operator-initiated status transitions.
# Rows are (from_status, to_status) pairs that are allowed.
_VALID_STATUS_TRANSITIONS: set[tuple[str, str]] = {
    ("active", "paused"),
    ("active", "draining"),
    ("paused", "active"),
    ("draining", "active"),
    ("draining", "paused"),
}


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


def _runner_int_telemetry(
    capabilities: Dict[str, Any] | None, key: str
) -> int | None:
    """Read a non-negative int telemetry value stashed on capabilities.

    Used for the display-only trust telemetry (``disk_free_bytes``,
    ``gpu_utilization_pct``) the runner reports on each heartbeat. Tolerant of
    absent/None/malformed values (old runners omit them).
    """
    raw = (capabilities or {}).get(key)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and raw.is_integer():
        return int(raw) if raw >= 0 else None
    return None


def _runner_float_telemetry(
    capabilities: Dict[str, Any] | None, key: str
) -> float | None:
    """Read a non-negative float telemetry value stashed on capabilities.

    Used for the display-only trust telemetry (``cpu_load_avg``) the runner
    reports on each heartbeat. Tolerant of absent/None/malformed values.
    """
    raw = (capabilities or {}).get(key)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if float(raw) >= 0.0 else None
    return None


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


def _runner_max_concurrent_compute(capabilities: Dict[str, Any] | None) -> int | None:
    """Max concurrent compute jobs, defaulting to max_concurrent when absent."""
    raw = (capabilities or {}).get("max_concurrent_compute")
    if raw is not None:
        if isinstance(raw, bool):
            pass  # fall through to default
        elif isinstance(raw, int) and raw > 0:
            return raw
        elif isinstance(raw, float) and raw.is_integer() and raw > 0:
            return int(raw)
        elif isinstance(raw, str):
            try:
                parsed = int(raw)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
    return _runner_max_concurrent(capabilities)


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
    max_concurrent_compute = _runner_max_concurrent_compute(runner.capabilities)
    available_slots = (
        max(0, max_concurrent - active_work_count)
        if max_concurrent is not None
        else None
    )
    available_agent_slots = (
        max(0, max_concurrent - active_task_count)
        if max_concurrent is not None
        else None
    )
    available_compute_slots = (
        max(0, max_concurrent_compute - active_compute_job_count)
        if max_concurrent_compute is not None
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

    # Trust telemetry: a live runner can still be failing every job it is handed.
    consecutive_job_failures = runner.consecutive_job_failures or 0
    if runner.last_successful_job_at is None:
        seconds_since_last_success = None
    else:
        seconds_since_last_success = max(
            0, int((now - runner.last_successful_job_at).total_seconds())
        )
    success_is_stale = (
        seconds_since_last_success is None
        or seconds_since_last_success >= _SUCCESS_STALE_SECONDS
    )
    failure_streak_degraded = (
        consecutive_job_failures >= _RUNNER_DEGRADED_FAILURE_THRESHOLD
    )
    success_stale_while_working = (
        active_work_count > 0
        and consecutive_job_failures > 0
        and success_is_stale
    )
    degraded = is_live and (failure_streak_degraded or success_stale_while_working)
    degraded_reason = None
    if degraded:
        degraded_reason = (
            "job_failure_streak" if failure_streak_degraded else "success_stale"
        )

    disk_free_bytes = _runner_int_telemetry(runner.capabilities, "disk_free_bytes")
    gpu_utilization_pct = _runner_int_telemetry(
        runner.capabilities, "gpu_utilization_pct"
    )
    disk_low = disk_free_bytes is not None and disk_free_bytes < _LOW_DISK_FREE_BYTES
    memory_free_bytes = _runner_int_telemetry(
        runner.capabilities, "memory_free_bytes"
    )
    memory_low = (
        memory_free_bytes is not None
        and memory_free_bytes < _LOW_MEMORY_FREE_BYTES
    )
    cpu_load_avg = _runner_float_telemetry(runner.capabilities, "cpu_load_avg")

    # device_status is the operator-facing tone. "degraded" is distinct from
    # "connected": the runner is live (recent heartbeat) but its jobs are failing,
    # so the console must NOT read it as ready. is_live stays true — a degraded
    # runner is still connected — so slot/capacity accounting is unaffected.
    if is_live and degraded:
        device_status = "degraded"
        device_status_reason = degraded_reason
    elif is_live:
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
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "seconds_since_last_seen": seconds_since_last_seen,
        "active_task_count": active_task_count,
        "active_compute_job_count": active_compute_job_count,
        "active_work_count": active_work_count,
        "max_concurrent": max_concurrent,
        "max_concurrent_compute": max_concurrent_compute,
        "available_slots": available_slots,
        "available_agent_slots": available_agent_slots,
        "available_compute_slots": available_compute_slots,
        "stale_reason": stale_reason,
        "agent_available": (runner.capabilities or {}).get("agent_available"),
        "agent_unavailable_reason": (runner.capabilities or {}).get("agent_unavailable_reason"),
        "liveness_window_seconds": _RUNNER_LIVENESS_SECONDS,
        # Trust telemetry surfaced to the panel.
        "last_successful_job_at": (
            runner.last_successful_job_at.isoformat()
            if runner.last_successful_job_at
            else None
        ),
        "seconds_since_last_success": seconds_since_last_success,
        "consecutive_job_failures": consecutive_job_failures,
        "disk_free_bytes": disk_free_bytes,
        "disk_low": disk_low,
        "memory_free_bytes": memory_free_bytes,
        "memory_low": memory_low,
        "cpu_load_avg": cpu_load_avg,
        "gpu_utilization_pct": gpu_utilization_pct,
        "failure_streak_threshold": _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    }


def _runner_view(runner: LocalRunner) -> Dict[str, Any]:
    now = _utcnow()
    token_age_days = (
        max(0, int((now - runner.token_created_at).total_seconds() / 86400))
        if runner.token_created_at
        else None
    )
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
        "token_age_days": token_age_days,
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runner = LocalRunner(
        project_id=project_id,
        token_hash="",  # filled below once we have the runner id for the token
        hostname=payload.hostname,
        version=payload.version,
        capabilities=capabilities,
    )
    token = create_runner_token(runner.id)
    runner.token_hash = hash_runner_token(token)
    runner.token_created_at = _utcnow()
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


@router.patch("/projects/{project_id}/runners/{runner_id}/capabilities")
async def update_runner_capabilities(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    payload: RunnerUpdateCapabilitiesRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update an existing runner's operator-configured capabilities in-place.

    Only the fields provided in the request body are updated; all other
    capabilities (including runner-reported hardware telemetry) are preserved.
    The ``strategic_local`` preset invariant is enforced: ``max_concurrent``
    is clamped to 1 and ``task_types`` is clamped to the strategic task set.
    """
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

    # Start from existing capabilities so runner-reported fields survive.
    caps = dict(runner.capabilities or {})

    # Detect strategic workload: strategic preset or workload pins concurrency.
    is_strategic = caps.get("runner_preset") in (
        "strategic_local",
    ) or caps.get("workload") == "strategic"

    try:
        if payload.max_concurrent is not None:
            mc = _normalize_max_concurrent(payload.max_concurrent)
            if is_strategic:
                mc = 1  # strategic runners are pinned to single concurrency
            caps["max_concurrent"] = mc

        if payload.task_types is not None:
            tt = _normalize_task_types(payload.task_types)
            if is_strategic:
                tt = list(_STRATEGIC_LOCAL_TASK_TYPES)
            caps["task_types"] = tt

        if payload.max_concurrent_compute is not None:
            mcc = _normalize_max_concurrent(payload.max_concurrent_compute)
            caps["max_concurrent_compute"] = mcc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runner.capabilities = caps
    session.add(runner)
    await session.commit()

    logger.info(
        "[RUNNERS] Updated capabilities for runner %s on project %s",
        runner_id,
        project_id,
    )
    await emit_workbench_refetch(
        project_id,
        resource="local_runner",
        action="updated",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="runner",
        summary={
            "id": str(runner_id),
            "status": runner.status,
            "hostname": runner.hostname,
        },
    )
    return _runner_view(runner)


@router.patch("/projects/{project_id}/runners/{runner_id}/status")
async def update_runner_status(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    payload: RunnerStatusUpdateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Pause, drain, or resume a runner.

    Valid transitions:
      active  → paused   (stop new dispatch immediately)
      active  → draining (stop new dispatch, let existing tasks finish)
      paused  → active   (resume)
      draining → active  (resume)
      draining → paused  (force-pause a draining runner)

    On pause or drain, any tasks pinned but not yet pulled by this runner are
    released back to QUEUED so another runner can claim them.
    """
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

    current = runner.status
    target = payload.status
    if current == target:
        # Already in the requested state — idempotent, no-op.
        return _runner_view(runner)

    if (current, target) not in _VALID_STATUS_TRANSITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status transition: {current!r} → {target!r}",
        )

    runner.status = target
    session.add(runner)

    released_pins = 0
    if target in ("paused", "draining"):
        released_pins = await _release_unpulled_runner_pins(session, runner_id)

    await session.commit()
    await session.refresh(runner)

    logger.info(
        "[RUNNERS] Status %s → %s for runner %s on project %s (released_pins=%s)",
        current,
        target,
        runner_id,
        project_id,
        released_pins,
    )
    await emit_workbench_refetch(
        project_id,
        resource="local_runner",
        action="status_updated",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="runner",
        summary={
            "id": str(runner_id),
            "status": target,
            "hostname": runner.hostname,
            "released_unpulled_pins": released_pins,
        },
    )
    return _runner_view(runner)


@router.get("/projects/{project_id}/runners")
async def list_runners(
    project_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List the project's runners (excluding deleted ones)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    query = (
        select(LocalRunner)
        .where(
            LocalRunner.project_id == project_id,
            LocalRunner.status != "deleted",
        )
        .order_by(LocalRunner.created_at.desc())
    )
    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    return [_runner_view(r) for r in result.scalars().all()]


def _snapshot_view(snap: RunnerHeartbeatSnapshot) -> Dict[str, Any]:
    return {
        "captured_at": snap.captured_at.isoformat() if snap.captured_at else None,
        "gpu_utilization_pct": snap.gpu_utilization_pct,
        "disk_free_bytes": snap.disk_free_bytes,
        "memory_free_bytes": snap.memory_free_bytes,
        "cpu_load_avg": snap.cpu_load_avg,
        "active_task_count": snap.active_task_count,
        "active_compute_job_count": snap.active_compute_job_count,
        "agent_available": snap.agent_available,
    }


@router.get("/projects/{project_id}/runners/{runner_id}/telemetry")
async def runner_telemetry(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    since: int = Query(
        default=3600, ge=60, le=604800, description="Look-back window in seconds"
    ),
    limit: int = Query(default=240, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return recent heartbeat time-series snapshots for one runner.

    Defaults to the last hour (``since=3600``), newest first, capped at ``limit``
    rows. Powers the Fleet panel's GPU/disk/memory sparklines. Read-only; a
    viewer role suffices.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    # 404 on a runner that is not this project's, so telemetry can't be read
    # cross-project even though snapshots carry no project_id of their own.
    runner = (
        await session.execute(
            select(LocalRunner).where(
                LocalRunner.id == runner_id,
                LocalRunner.project_id == project_id,
            )
        )
    ).scalars().first()
    if runner is None or runner.status == "deleted":
        raise HTTPException(status_code=404, detail="Runner not found")

    cutoff = _utcnow() - timedelta(seconds=since)
    rows = (
        await session.execute(
            select(RunnerHeartbeatSnapshot)
            .where(
                RunnerHeartbeatSnapshot.runner_id == runner_id,
                RunnerHeartbeatSnapshot.captured_at >= cutoff,
            )
            .order_by(RunnerHeartbeatSnapshot.captured_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "runner_id": str(runner_id),
        "since_seconds": since,
        "count": len(rows),
        # Newest-first from the query; return oldest-first so the UI can plot
        # left-to-right without reversing.
        "snapshots": [_snapshot_view(s) for s in reversed(rows)],
    }


@router.get("/projects/{project_id}/runners/{runner_id}/stats")
async def runner_stats(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    recent_limit: int = Query(default=8, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-runner compute-job rollups: total jobs, success rate, recent outcomes.

    Compute jobs are pinned to a runner via
    ``polling_metadata['runner_target_id']`` (there is no ComputeJob.runner_id
    column), so both the count and the recent-outcomes list filter on that key.
    Success rate is succeeded / terminal (terminal = succeeded|failed|cancelled|
    timed_out); non-terminal jobs are excluded from the denominator. Read-only.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    runner = (
        await session.execute(
            select(LocalRunner).where(
                LocalRunner.id == runner_id,
                LocalRunner.project_id == project_id,
            )
        )
    ).scalars().first()
    if runner is None or runner.status == "deleted":
        raise HTTPException(status_code=404, detail="Runner not found")

    terminal_statuses = {
        ComputeJobStatus.SUCCEEDED,
        ComputeJobStatus.FAILED,
        ComputeJobStatus.CANCELLED,
        ComputeJobStatus.TIMED_OUT,
    }

    runner_id_str = str(runner_id)
    pin_condition = ComputeJob.polling_metadata["runner_target_id"].as_string() == runner_id_str
    base_filter = [ComputeJob.project_id == project_id, pin_condition]

    # Aggregate counts via SQL — avoids loading every row into Python memory
    total_result = await session.execute(
        select(func.count(ComputeJob.id)).where(*base_filter)
    )
    jobs_run_count = total_result.scalar() or 0

    terminal_result = await session.execute(
        select(func.count(ComputeJob.id)).where(
            *base_filter,
            ComputeJob.status.in_(terminal_statuses),
        )
    )
    jobs_terminal_count = terminal_result.scalar() or 0

    succeeded_result = await session.execute(
        select(func.count(ComputeJob.id)).where(
            *base_filter,
            ComputeJob.status == ComputeJobStatus.SUCCEEDED,
        )
    )
    jobs_succeeded_count = succeeded_result.scalar() or 0

    success_rate = (
        (jobs_succeeded_count / jobs_terminal_count)
        if jobs_terminal_count > 0
        else None
    )

    # Cloud cost avoided — the on-demand cloud VM cost that terminal jobs run on
    # this self-hosted runner *would* have incurred. We only have honest wall
    # clock for jobs with both started_at and completed_at, so those are the
    # basis. Runtime seconds are summed per (machine_type, accelerator) group and
    # priced at the on-demand (non-spot) rate; unpriceable groups (unsupported
    # accelerator names) contribute nothing rather than failing the whole read.
    cost_rows = (
        await session.execute(
            select(
                ComputeJob.machine_type,
                ComputeJob.accelerator_type,
                ComputeJob.accelerator_count,
                ComputeJob.started_at,
                ComputeJob.completed_at,
            ).where(
                *base_filter,
                ComputeJob.status.in_(terminal_statuses),
                ComputeJob.started_at.is_not(None),
                ComputeJob.completed_at.is_not(None),
            )
        )
    ).all()

    # (machine_type, accelerator_type, accelerator_count) -> summed runtime secs
    seconds_by_group: Dict[tuple, float] = {}
    cloud_cost_avoided_basis = 0
    for machine_type, accel_type, accel_count, started_at, completed_at in cost_rows:
        runtime_secs = (completed_at - started_at).total_seconds()
        if runtime_secs <= 0:
            # Clock skew or zero-length window — no honest cost basis.
            continue
        key = (machine_type, accel_type, accel_count)
        seconds_by_group[key] = seconds_by_group.get(key, 0.0) + runtime_secs
        cloud_cost_avoided_basis += 1

    cloud_cost_avoided_usd = 0.0
    for (machine_type, accel_type, accel_count), total_secs in seconds_by_group.items():
        try:
            rate_per_second = get_compute_rate_per_second(
                machine_type=machine_type,
                accelerator=accel_type,
                count=accel_count or 1,
                spot=False,
            )
        except AcceleratorRejection:
            # Unsupported/unknown accelerator shape — cannot price it, so it
            # does not contribute to the avoided-cost total.
            continue
        cloud_cost_avoided_usd += total_secs * rate_per_second
    cloud_cost_avoided_usd = round(cloud_cost_avoided_usd, 2)

    # Claim-latency rollup from task-level cold-start telemetry.
    # Tasks dispatched to this runner have vm_instance_id = "runner:{runner_id}".
    # We query terminal tasks with a recorded vm_cold_start_ms to compute
    # per-runner pickup-lag percentiles.
    claim_latency_ms_values = (
        (
            await session.execute(
                select(Task.vm_cold_start_ms).where(
                    Task.vm_instance_id == f"runner:{runner_id_str}",
                    Task.vm_cold_start_ms.is_not(None),
                    Task.status.in_(TASK_OUTCOME_TERMINAL_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )

    claim_latency_ms_p50: int | None = None
    claim_latency_ms_p95: int | None = None
    claim_latency_ms_p99: int | None = None
    claim_latency_ms_max: int | None = None
    claim_latency_sample_count = len(claim_latency_ms_values)

    if claim_latency_ms_values:
        sorted_vals = sorted(claim_latency_ms_values)
        n = len(sorted_vals)
        claim_latency_ms_max = sorted_vals[-1]
        claim_latency_ms_p50 = sorted_vals[min(int(n * 0.5), n - 1)]
        claim_latency_ms_p95 = sorted_vals[min(int(n * 0.95), n - 1)]
        claim_latency_ms_p99 = sorted_vals[min(int(n * 0.99), n - 1)]

    # Recent outcomes — bounded fetch
    recent_jobs = (
        await session.execute(
            select(ComputeJob)
            .where(*base_filter)
            .order_by(ComputeJob.created_at.desc())
            .limit(recent_limit)
        )
    ).scalars().all()

    last_outcomes = [
        {
            "job_id": str(j.id),
            "task_id": str(j.task_id) if j.task_id else None,
            "status": j.status.value if hasattr(j.status, "value") else str(j.status),
            "outcome": (
                j.outcome.value if getattr(j, "outcome", None) is not None else None
            ),
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        }
        for j in recent_jobs
    ]

    return {
        "runner_id": str(runner_id),
        "jobs_run_count": jobs_run_count,
        "jobs_terminal_count": jobs_terminal_count,
        "jobs_succeeded_count": jobs_succeeded_count,
        "jobs_success_rate": success_rate,
        "cloud_cost_avoided_usd": cloud_cost_avoided_usd,
        "cloud_cost_avoided_basis": cloud_cost_avoided_basis,
        "claim_latency_ms_p50": claim_latency_ms_p50,
        "claim_latency_ms_p95": claim_latency_ms_p95,
        "claim_latency_ms_p99": claim_latency_ms_p99,
        "claim_latency_ms_max": claim_latency_ms_max,
        "claim_latency_sample_count": claim_latency_sample_count,
        "last_n_outcomes": last_outcomes,
    }


@router.post("/projects/{project_id}/runners/{runner_id}/rotate-token")
async def rotate_runner_token(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Rotate a runner secret and return the new plaintext token exactly once."""
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

    token = create_runner_token(runner.id)
    runner.token_hash = hash_runner_token(token)
    runner.token_created_at = _utcnow()
    summary_status = runner.status
    summary_hostname = runner.hostname
    session.add(runner)
    await session.commit()

    logger.info(
        "[RUNNERS] Rotated token for runner %s on project %s",
        runner_id,
        project_id,
    )
    await emit_workbench_refetch(
        project_id,
        resource="local_runner",
        action="token_rotated",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="runner",
        summary={
            "id": str(runner_id),
            "status": summary_status,
            "hostname": summary_hostname,
        },
    )
    return {"runner_id": str(runner_id), "token": token}


async def _release_unpulled_runner_pins(
    session: AsyncSession,
    runner_id: uuid.UUID,
) -> int:
    """Release tasks pinned to a runner before the runner pulled them.

    Uses batched LIMIT queries to avoid loading thousands of pinned tasks
    into memory at once (following the same pattern as projects.py for
    batched task cancellation).
    """
    total = 0
    _BATCH_SIZE = 500
    while True:
        pinned = (
            await session.execute(
                select(Task).where(
                    Task.status == TaskStatus.PROVISIONING,
                    Task.started_at.is_(None),
                    Task.vm_instance_id == f"runner:{runner_id}",
                ).limit(_BATCH_SIZE)
            )
        ).scalars().all()
        if not pinned:
            break
        for task in pinned:
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = None
            task.vm_provisioned_at = None
            new_ctx = dict(task.context or {})
            new_ctx.pop("runner_target_id", None)
            task.context = new_ctx
            session.add(task)
        total += len(pinned)
        await session.flush()
    return total


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


@router.get("/fleet/runners/health-summary")
async def fleet_runners_health_summary(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Fleet-wide runner-health summary across every project.

    Returns aggregate counts (total, connected, degraded, stale, offline,
    low-disk) plus a per-project breakdown that reuses ``_runner_health()`` so
    the fleet view and the per-project runner list can never diverge. Guarded
    with ``require_admin`` — the same decorator as the other fleet-wide admin
    endpoints in ``admin_health.py``.
    """
    now = _utcnow()

    # Cap the detailed breakdown at a sensible limit to prevent unbounded
    # payload growth as the fleet scales. The aggregate counts reflect only
    # the loaded subset; if the fleet exceeds this cap, ``limit_reached``
    # is set so consumers know the details are a sample.
    _MAX_HEALTH_RUNNERS = 1000
    result = await session.execute(
        select(LocalRunner)
        .where(LocalRunner.status != "deleted")
        .limit(_MAX_HEALTH_RUNNERS)
    )
    all_runners = result.scalars().all()
    if len(all_runners) == _MAX_HEALTH_RUNNERS:
        logger.warning(
            "[FLEET-HEALTH] health-summary hit limit=%d; some runners excluded",
            _MAX_HEALTH_RUNNERS,
        )

    per_project: Dict[str, List[LocalRunner]] = {}
    for runner in all_runners:
        pid = str(runner.project_id)
        per_project.setdefault(pid, []).append(runner)

    fleet = {
        "total": 0,
        "connected": 0,
        "degraded": 0,
        "stale": 0,
        "offline": 0,
        "low_disk": 0,
        "low_memory": 0,
    }
    per_project_entries: List[Dict[str, Any]] = []

    for pid, project_runners in sorted(per_project.items()):
        entry: Dict[str, Any] = {
            "project_id": pid,
            "total": 0,
            "connected": 0,
            "degraded": 0,
            "stale": 0,
            "offline": 0,
            "low_disk": 0,
            "low_memory": 0,
            "runners": [],
        }
        for runner in project_runners:
            view = _runner_view(runner)
            entry["runners"].append(view)
            entry["total"] += 1
            status = view["health"]["device_status"]
            if status == "connected":
                entry["connected"] += 1
            elif status == "degraded":
                entry["degraded"] += 1
            elif status == "stale":
                entry["stale"] += 1
            elif status == "offline":
                entry["offline"] += 1
            if view["health"].get("disk_low"):
                entry["low_disk"] += 1
            if view["health"].get("memory_low"):
                entry["low_memory"] += 1

        for key in ("total", "connected", "degraded", "stale", "offline", "low_disk", "low_memory"):
            fleet[key] += entry[key]

        per_project_entries.append(entry)

    fleet["per_project"] = per_project_entries
    fleet["generated_at"] = now.isoformat()
    fleet["project_count"] = len(per_project_entries)
    fleet["limit_reached"] = len(all_runners) >= _MAX_HEALTH_RUNNERS
    fleet["max_runners"] = _MAX_HEALTH_RUNNERS
    return fleet
