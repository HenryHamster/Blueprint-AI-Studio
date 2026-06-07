"""Persistence and monitoring for external compute jobs.

The persisted :class:`app.models.ComputeJob` row is the single source of truth.
Substrate-specific runtime data the schema has no dedicated column for (launch
command spec, provisioning model, per-second cost rate, GCP zone, exit code)
lives in ``ComputeJob.polling_metadata``; terminal/outcome state uses the
upstream ``status`` / ``outcome`` / ``terminal_reason`` columns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import select

from app.compute_backends import (
    ComputeLaunchSpec,
    default_compute_backend_name,
    get_compute_backend,
    validate_backend_name,
)
from app.database import AsyncSessionLocal
from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
from app.vm_cost_rates import get_vm_rate_per_second, is_supported_machine_type

logger = logging.getLogger(__name__)

MIN_COMPUTE_JOB_SPEND_CAP_USD = float(
    os.getenv("COMPUTE_JOB_MIN_SPEND_CAP_USD", "0.01")
)
MAX_COMPUTE_JOB_SPEND_CAP_USD = float(
    os.getenv("COMPUTE_JOB_MAX_SPEND_CAP_USD", "100.0")
)
MIN_COMPUTE_JOB_DEADLINE_SECONDS = int(
    os.getenv("COMPUTE_JOB_MIN_DEADLINE_SECONDS", "60")
)
MAX_COMPUTE_JOB_DEADLINE_SECONDS = int(
    os.getenv("COMPUTE_JOB_MAX_DEADLINE_SECONDS", "86400")
)
COMPUTE_JOB_MONITOR_INTERVAL_SECONDS = float(
    os.getenv("COMPUTE_JOB_MONITOR_INTERVAL_SECONDS", "15")
)

_TASK_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}
_COMPUTE_JOB_TERMINAL_STATUSES = {
    ComputeJobStatus.SUCCEEDED,
    ComputeJobStatus.FAILED,
    ComputeJobStatus.CANCELLED,
    ComputeJobStatus.TIMED_OUT,
}
_COMPUTE_JOB_ACTIVE_STATUSES = [
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
    ComputeJobStatus.RUNNING,
    ComputeJobStatus.POLLING,
]
# Statuses a job can be in before the external launch side-effect has been
# observed to succeed (RUNNING). STARTING is reached only after a durable commit.
_COMPUTE_JOB_PRELAUNCH_STATUSES = {
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
}


def clamp_spend_cap_usd(raw_value: float) -> float:
    return max(
        MIN_COMPUTE_JOB_SPEND_CAP_USD,
        min(float(raw_value), MAX_COMPUTE_JOB_SPEND_CAP_USD),
    )


def clamp_deadline_seconds(raw_value: int) -> int:
    return max(
        MIN_COMPUTE_JOB_DEADLINE_SECONDS,
        min(int(raw_value), MAX_COMPUTE_JOB_DEADLINE_SECONDS),
    )


def normalize_provisioning_model(raw_value: str | None) -> str:
    value = (raw_value or "STANDARD").upper()
    if value not in {"STANDARD", "SPOT"}:
        raise ValueError("provisioning_model must be STANDARD or SPOT")
    return value


async def create_compute_job(
    session,
    task: Task,
    *,
    command: str,
    machine_type: str,
    spend_cap_usd: float,
    deadline_seconds: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    backend_name: str | None = None,
    provisioning_model: str | None = None,
) -> ComputeJob:
    if not command.strip():
        raise ValueError("command must be non-empty")
    if not is_supported_machine_type(machine_type):
        raise ValueError(f"unsupported machine_type: {machine_type}")

    normalized_backend = backend_name or default_compute_backend_name()
    # Validate BEFORE persisting: an unknown / mode-incompatible backend would
    # otherwise wedge the row in QUEUED forever (monitor raises every pass).
    validate_backend_name(normalized_backend)
    normalized_provisioning = normalize_provisioning_model(provisioning_model)
    capped_spend = clamp_spend_cap_usd(spend_cap_usd)
    capped_deadline = clamp_deadline_seconds(deadline_seconds)
    launch_request_fingerprint = _build_launch_request_fingerprint(
        command=command,
        machine_type=machine_type,
        spend_cap_usd=capped_spend,
        deadline_seconds=capped_deadline,
        cwd=cwd,
        env=env,
        backend_name=normalized_backend,
        provisioning_model=normalized_provisioning,
    )
    locked_task = await _lock_task_for_compute_job(session, task.id)
    if locked_task is None:
        raise ValueError("task not found")
    if locked_task.status in _TASK_TERMINAL_STATUSES:
        raise ValueError("cannot launch a compute job for a terminal task")
    existing = await _get_active_compute_job_for_task(
        session, task_id=locked_task.id
    )
    if existing is not None:
        if _launch_request_matches(existing, launch_request_fingerprint):
            return existing
        raise ValueError("task already has an active compute job")
    deadline_at = _utcnow() + timedelta(seconds=capped_deadline)
    rate = get_vm_rate_per_second(
        machine_type=machine_type,
        spot=normalized_provisioning == "SPOT",
    )
    job = ComputeJob(
        task_id=locked_task.id,
        project_id=locked_task.project_id,
        backend=normalized_backend,
        status=ComputeJobStatus.QUEUED,
        machine_type=machine_type,
        spend_cap_usd=capped_spend,
        accrued_cost_usd=0.0,
        wall_clock_deadline=deadline_at,
        polling_metadata={
            "command": command,
            "cwd": cwd,
            "env": {str(k): str(v) for k, v in (env or {}).items()},
            "provisioning_model": normalized_provisioning,
            "cost_rate_per_second": rate,
            "requested_deadline_seconds": capped_deadline,
            "launch_request_fingerprint": launch_request_fingerprint,
            "status_message": "queued for compute-job launch",
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _lock_task_for_compute_job(session, task_id: uuid.UUID | None) -> Task | None:
    if task_id is None:
        return None
    result = await session.execute(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def _get_active_compute_job_for_task(
    session, *, task_id: uuid.UUID
) -> ComputeJob | None:
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.task_id == task_id,
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES),
        )
        .order_by(ComputeJob.created_at)
    )
    return result.scalars().first()


def _build_launch_request_fingerprint(
    *,
    command: str,
    machine_type: str,
    spend_cap_usd: float,
    deadline_seconds: int,
    cwd: str | None,
    env: dict[str, str] | None,
    backend_name: str,
    provisioning_model: str,
) -> str:
    payload = {
        "backend": backend_name,
        "command": command,
        "cwd": cwd,
        "deadline_seconds": deadline_seconds,
        "env": {str(k): str(v) for k, v in (env or {}).items()},
        "machine_type": machine_type,
        "provisioning_model": provisioning_model,
        "spend_cap_usd": float(spend_cap_usd),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _launch_request_matches(job: ComputeJob, request_fingerprint: str) -> bool:
    existing = str(
        (job.polling_metadata or {}).get("launch_request_fingerprint") or ""
    ).strip()
    return bool(existing) and existing == request_fingerprint


async def get_compute_job_for_task(
    session, *, job_id: uuid.UUID, task_id: uuid.UUID
) -> ComputeJob | None:
    result = await session.execute(
        select(ComputeJob).where(
            ComputeJob.id == job_id,
            ComputeJob.task_id == task_id,
        )
    )
    return result.scalars().first()


async def stamp_compute_job_result_reference(
    session,
    *,
    task_id: uuid.UUID,
    job_id: uuid.UUID,
    result_reference: str | None,
) -> ComputeJob | None:
    """Persist a terminal result reference on the matching compute-job row."""
    reference = (result_reference or "").strip()
    if not reference:
        return None
    job = await get_compute_job_for_task(session, job_id=job_id, task_id=task_id)
    if job is None:
        return None
    job.result_reference = reference
    job.updated_at = _utcnow()
    session.add(job)
    return job


async def _lock_compute_job(session, job_id: uuid.UUID) -> ComputeJob | None:
    result = await session.execute(
        select(ComputeJob)
        .where(ComputeJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def kill_compute_job(
    session,
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    killed_by: str | None = None,
) -> ComputeJob:
    """Operator-initiated kill of an active compute job.

    Locks the row, terminates the external compute via the backend, and drives
    the job to the same terminal shape the monitor produces (CANCELLED status +
    CANCELLED outcome) so the operator kill is distinguishable from a budget
    (SPEND_CAP_EXCEEDED) or deadline (TIMED_OUT) teardown by outcome, and from a
    task-terminal cancel by the reason text. Rejects an already-terminal job.
    """
    job = await _lock_compute_job(session, job_id)
    if job is None or job.project_id != project_id:
        raise ValueError("compute job not found")
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        raise ValueError("compute job is already terminal")

    now = _utcnow()
    backend = get_compute_backend(job.backend)
    terminated = True
    if job.external_compute_id:
        terminated = await backend.terminate(job, reason="operator")
    by = f" ({killed_by})" if killed_by else ""
    _finalize_terminal_job(
        job,
        status=ComputeJobStatus.CANCELLED,
        outcome=ComputeJobOutcome.CANCELLED,
        terminal_reason=f"killed by operator{by}",
        now=now,
        terminate_ok=terminated,
    )
    await session.commit()
    await session.refresh(job)
    return job


async def list_compute_jobs(
    session,
    *,
    project_id: uuid.UUID,
    include_finished_within: timedelta = timedelta(hours=6),
) -> list[ComputeJob]:
    """Return a project's active compute jobs plus recently-finished ones.

    Active jobs are always included; terminal jobs are included only when they
    completed within ``include_finished_within``. Ordered newest-first so the
    live operator view leads with the most recent launches.
    """
    cutoff = _utcnow() - include_finished_within
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.project_id == project_id,
            (
                ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES)
                | (ComputeJob.completed_at.is_not(None) & (ComputeJob.completed_at >= cutoff))
            ),
        )
        .order_by(ComputeJob.created_at.desc())
    )
    return list(result.scalars().all())


async def get_recent_terminal_compute_spend_usd(
    session,
    *,
    project_id: uuid.UUID | None,
    since: datetime,
    until: datetime | None = None,
) -> float:
    """Sum ``final_cost_usd`` over terminal compute jobs in a time window.

    The single source of truth for "how much off-VM compute did we spend
    recently". Counts only jobs that have reached a terminal status
    (``_COMPUTE_JOB_TERMINAL_STATUSES``) and carry a recorded
    ``final_cost_usd``, whose ``completed_at`` falls in ``[since, until)``
    (``until`` is exclusive; omit it for an open-ended upper bound). In-flight
    jobs — non-terminal status, no ``completed_at``, or ``final_cost_usd``
    still ``None`` — are ignored so the figure reflects settled spend only.
    Scoped to ``project_id`` when provided; ``None`` aggregates across projects.
    """
    conditions = [
        ComputeJob.status.in_(_COMPUTE_JOB_TERMINAL_STATUSES),
        ComputeJob.completed_at.is_not(None),
        ComputeJob.completed_at >= since,
        ComputeJob.final_cost_usd.is_not(None),
    ]
    if project_id is not None:
        conditions.append(ComputeJob.project_id == project_id)
    if until is not None:
        conditions.append(ComputeJob.completed_at < until)
    result = await session.execute(
        select(func.coalesce(func.sum(ComputeJob.final_cost_usd), 0.0)).where(
            *conditions
        )
    )
    return float(result.scalar_one() or 0.0)


async def active_compute_job_vm_ids(session) -> set[str]:
    """External ids of non-terminal compute jobs.

    The generic GCP orphan sweep (now ``compute-job-vm-*`` aware) must treat a
    running compute job's VM as active so it isn't deleted out from under the
    monitor; only once the job is terminal does its VM become a reclaimable
    orphan.
    """
    result = await session.execute(
        select(ComputeJob.external_compute_id).where(
            ComputeJob.external_compute_id.is_not(None),
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES),
        )
    )
    return {vid for vid in result.scalars().all() if vid}


async def process_compute_jobs_once(session_factory=AsyncSessionLocal) -> None:
    async with session_factory() as session:
        result = await session.execute(
            select(ComputeJob)
            .where(ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES))
            .order_by(ComputeJob.created_at)
        )
        jobs = result.scalars().all()

    for job in jobs:
        async with session_factory() as session:
            live = await session.get(ComputeJob, job.id)
            if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
                continue
            try:
                await _process_single_compute_job(session, live)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.exception(
                    "[COMPUTE_JOB] monitor failed for job=%s: %s",
                    live.id,
                    exc,
                )


async def compute_job_monitor_service(session_factory=AsyncSessionLocal) -> None:
    """Leader-only monitor: launch pending jobs, accrue cost, enforce caps."""
    while True:
        await process_compute_jobs_once(session_factory=session_factory)
        await asyncio.sleep(COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)


async def _process_single_compute_job(session, job: ComputeJob) -> None:
    now = _utcnow()
    backend = get_compute_backend(job.backend)

    task = await session.get(Task, job.task_id) if job.task_id else None
    if job.task_id is not None and (
        task is None or task.status in _TASK_TERMINAL_STATUSES
    ):
        if job.external_compute_id:
            await backend.terminate(job, reason="task_terminal")
        _finalize_terminal_job(
            job,
            status=ComputeJobStatus.CANCELLED,
            outcome=ComputeJobOutcome.CANCELLED,
            terminal_reason="owning task is terminal; compute job cancelled",
            now=now,
        )
        return

    if job.status in _COMPUTE_JOB_PRELAUNCH_STATUSES:
        await _launch_compute_job(session, job, backend, now)
        return

    # ── Running: poll STATUS first, then cost. A job that finished just before
    # this tick must be billed through finished_at, not through poll time, so
    # spend-cap decisions are never made from an inflated accrual. ──
    status = await backend.poll_status(job)
    job.poll_attempt_count = (job.poll_attempt_count or 0) + 1
    job.last_polled_at = now
    job.next_poll_at = now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)

    if status.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        finished = status.finished_at or now
        cost = await backend.poll_cost(job, as_of=finished)
        job.accrued_cost_usd = max(float(job.accrued_cost_usd or 0.0), cost.total_cost_usd)
        # Persist any command-reported result metrics under a dedicated
        # polling_metadata key. polling_metadata already serializes to the agent
        # via serialize_compute_job_response, so the metrics reach the handler
        # without a migration. Absent metrics (None) leave the key unwritten.
        if status.metrics:
            _set_meta(job, result_metrics=dict(status.metrics))
        outcome = (
            ComputeJobOutcome.SUCCESS
            if status.status == ComputeJobStatus.SUCCEEDED
            else ComputeJobOutcome.FAILURE
        )
        _finalize_terminal_job(
            job,
            status=status.status,
            outcome=outcome,
            terminal_reason=status.message
            or _meta_message(job)
            or "compute job finished",
            now=finished,
            exit_code=status.exit_code,
        )
        return

    # Still running: accrue cost up to now, then enforce deadline/budget.
    cost = await backend.poll_cost(job, as_of=now)
    job.accrued_cost_usd = max(float(job.accrued_cost_usd or 0.0), cost.total_cost_usd)

    if job.wall_clock_deadline is not None and job.wall_clock_deadline <= now:
        terminated = await backend.terminate(job, reason="deadline")
        _finalize_terminal_job(
            job,
            status=ComputeJobStatus.TIMED_OUT,
            outcome=ComputeJobOutcome.TIMED_OUT,
            terminal_reason="compute job terminated at deadline",
            now=now,
            terminate_ok=terminated,
        )
        return

    if job.spend_cap_usd is not None and float(job.accrued_cost_usd or 0.0) >= float(
        job.spend_cap_usd
    ):
        terminated = await backend.terminate(job, reason="budget")
        _finalize_terminal_job(
            job,
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.SPEND_CAP_EXCEEDED,
            terminal_reason="compute job terminated at spend cap",
            now=now,
            terminate_ok=terminated,
        )
        return

    job.status = ComputeJobStatus.RUNNING
    if status.message:
        _set_meta(job, status_message=status.message)


async def _launch_compute_job(session, job: ComputeJob, backend, now) -> None:
    """Launch a job, recording durable intent BEFORE the external side-effect.

    A QUEUED job is transitioned to STARTING with a deterministic reserved
    external id and committed *before* ``backend.launch`` runs. If the worker
    dies after launch but before the RUNNING commit, the next pass re-enters with
    the row already STARTING and re-launches against the same reserved id — the
    backends make that idempotent (GCP adopts the existing VM), so an external
    compute job is never launched twice and the budget is never double-spent.
    """
    if job.status == ComputeJobStatus.QUEUED:
        job.status = ComputeJobStatus.STARTING
        if not job.external_compute_id:
            job.external_compute_id = backend.external_id_for(job)
        job.updated_at = now
        await session.commit()  # durable: STARTING + reserved id before side-effect

    result = await backend.launch(_build_launch_spec(job))
    if result.external_id and not job.external_compute_id:
        job.external_compute_id = result.external_id
    job.started_at = job.started_at or result.started_at
    job.status = ComputeJobStatus.RUNNING
    job.last_polled_at = now
    job.next_poll_at = now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)
    extra: dict[str, Any] = dict(result.metadata or {})
    if result.cost_rate_per_second and result.cost_rate_per_second > 0:
        extra["cost_rate_per_second"] = result.cost_rate_per_second
    extra["status_message"] = result.message or "compute job launched"
    _set_meta(job, **extra)


def _build_launch_spec(job: ComputeJob) -> ComputeLaunchSpec:
    meta = job.polling_metadata or {}
    return ComputeLaunchSpec(
        job_id=job.id,
        external_id=job.external_compute_id,
        command=str(meta.get("command") or ""),
        machine_type=job.machine_type or "",
        spend_cap_usd=float(job.spend_cap_usd or 0.0),
        deadline_at=job.wall_clock_deadline,
        cwd=meta.get("cwd"),
        env={str(k): str(v) for k, v in (meta.get("env") or {}).items()},
        provisioning_model=str(meta.get("provisioning_model") or "STANDARD"),
    )


def _meta_message(job: ComputeJob) -> str | None:
    return (job.polling_metadata or {}).get("status_message")


def _set_meta(job: ComputeJob, *, status_message: str | None = None, **extra: Any) -> None:
    meta = dict(job.polling_metadata or {})
    if status_message is not None:
        meta["status_message"] = status_message
    meta.update(extra)
    job.polling_metadata = meta


def _finalize_terminal_job(
    job: ComputeJob,
    *,
    status: ComputeJobStatus,
    outcome: ComputeJobOutcome,
    terminal_reason: str,
    now,
    exit_code: int | None = None,
    terminate_ok: bool = True,
) -> None:
    job.status = status
    job.outcome = outcome
    job.completed_at = now
    job.last_polled_at = job.last_polled_at or now
    job.terminal_reason = terminal_reason
    job.final_cost_usd = job.accrued_cost_usd
    job.updated_at = now
    extra: dict[str, Any] = {"terminal_status": status.value}
    if exit_code is not None:
        extra["exit_code"] = exit_code
    if not terminate_ok:
        # The immediate delete retries failed; the orphan sweep is the safety net.
        extra["terminate_failed"] = True
    _set_meta(job, status_message=terminal_reason, **extra)
