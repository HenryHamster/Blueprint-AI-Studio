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
    LOCAL_BACKEND_NAME,
    ComputeLaunchSpec,
    collect_terminal_artifact_reference,
    default_compute_backend_name,
    get_compute_backend,
    validate_backend_name,
)
from app.artifact_prerequisites import (
    deliver_artifact_to_dependents,
    downstream_artifact_logical_key,
    stamp_artifact_logical_key,
)
from app.database import AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow
from app.vm_cost_rates import (
    get_compute_rate_per_second,
    resolve_compute_machine_type,
    validate_compute_request,
)

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

# Wake-source kind for the task park-and-wake primitive. A compute_experiment
# task parks on this kind with ``wake_source_ref=str(job.id)`` after launching
# its budgeted job instead of poll-pinning its agent VM; the monitor fires the
# wake EXACTLY ONCE when the job reaches a terminal state, and the leader-owned
# wake-delivery worker finalizes the parked task through the compute_experiment
# completion path. Mirrored by the agent handler and ``deliver_wake``.
COMPUTE_JOB_TERMINAL_WAKE_KIND = "compute_job_terminal"

# How each terminal ComputeJobOutcome maps to the (intentional, killed_by)
# disposition the agent used to compute client-side. SUCCESS, the budget/deadline
# kills, and CANCELLED are all INTENTIONAL terminal outcomes (the substrate did
# its job); only a genuine command FAILURE — or an unknown outcome — is a
# non-intentional failure.
_COMPUTE_OUTCOME_DISPOSITION: dict[ComputeJobOutcome, tuple[bool, str | None]] = {
    ComputeJobOutcome.SUCCESS: (True, None),
    ComputeJobOutcome.CANCELLED: (True, None),
    ComputeJobOutcome.TIMED_OUT: (True, "deadline"),
    ComputeJobOutcome.SPEND_CAP_EXCEEDED: (True, "budget"),
    ComputeJobOutcome.FAILURE: (False, None),
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
    machine_type: str | None = None,
    spend_cap_usd: float,
    deadline_seconds: int,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    backend_name: str | None = None,
    provisioning_model: str | None = None,
    accelerator: str | None = None,
    accelerator_count: int = 1,
) -> ComputeJob:
    if not command.strip():
        raise ValueError("command must be non-empty")
    resolved_machine_type = resolve_compute_machine_type(
        accelerator=accelerator,
        machine_type=machine_type,
    )
    # Single gate for CPU and GPU shapes: a GPU machine type is accepted ONLY when
    # paired with a supported single-GPU accelerator (rejects A100/over-quota,
    # count != 1, unknown accelerators, and GPU/machine-family mismatch); a CPU
    # request still requires an explicitly priced e2 machine type, unchanged.
    accel_spec, reject_reason = validate_compute_request(
        accelerator, resolved_machine_type, accelerator_count
    )
    if reject_reason is not None:
        raise ValueError(reject_reason)

    normalized_backend = backend_name or default_compute_backend_name()
    # Validate BEFORE persisting: an unknown / mode-incompatible backend would
    # otherwise wedge the row in QUEUED forever (monitor raises every pass).
    validate_backend_name(normalized_backend)
    # A GPU can never be provisioned by the local-subprocess backend — reject
    # loudly at create time rather than silently running the command CPU-only.
    if accel_spec is not None and normalized_backend == LOCAL_BACKEND_NAME:
        raise ValueError(
            "GPU compute jobs are unavailable on the local-subprocess backend"
        )
    normalized_provisioning = normalize_provisioning_model(provisioning_model)
    capped_spend = clamp_spend_cap_usd(spend_cap_usd)
    capped_deadline = clamp_deadline_seconds(deadline_seconds)
    # Persist the canonical accelerator short name (e.g. "T4") so the launch spec,
    # cost rate, and provisioning path all resolve the same supported shape.
    persisted_accelerator = accel_spec.name if accel_spec is not None else None
    persisted_accelerator_count = accelerator_count if accel_spec is not None else None
    launch_request_fingerprint = _build_launch_request_fingerprint(
        command=command,
        machine_type=resolved_machine_type,
        spend_cap_usd=capped_spend,
        deadline_seconds=capped_deadline,
        cwd=cwd,
        env=env,
        backend_name=normalized_backend,
        provisioning_model=normalized_provisioning,
        accelerator=persisted_accelerator,
        accelerator_count=persisted_accelerator_count,
    )
    locked_task = await _lock_task_for_compute_job(session, task.id)
    if locked_task is None:
        raise ValueError("task not found")
    if locked_task.status in _TASK_TERMINAL_STATUSES:
        raise ValueError("cannot launch a compute job for a terminal task")
    existing = await _get_active_compute_job_for_task(session, task_id=locked_task.id)
    if existing is not None:
        if _launch_request_matches(existing, launch_request_fingerprint):
            return existing
        raise ValueError("task already has an active compute job")
    deadline_at = _utcnow() + timedelta(seconds=capped_deadline)
    # Accelerator-aware rate: base machine + GPU surcharge (N1+T4/V100) or the
    # bundled G2 rate (L4); falls through to the plain VM rate for CPU jobs.
    rate = get_compute_rate_per_second(
        machine_type=resolved_machine_type,
        accelerator=persisted_accelerator,
        count=accelerator_count,
        spot=normalized_provisioning == "SPOT",
    )
    job = ComputeJob(
        task_id=locked_task.id,
        project_id=locked_task.project_id,
        backend=normalized_backend,
        status=ComputeJobStatus.QUEUED,
        machine_type=resolved_machine_type,
        accelerator_type=persisted_accelerator,
        accelerator_count=persisted_accelerator_count,
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
    accelerator: str | None = None,
    accelerator_count: int | None = None,
) -> str:
    payload = {
        "accelerator": accelerator,
        "accelerator_count": accelerator_count,
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
    """Persist a terminal result reference on the matching compute-job row.

    When the producer declared downstream_artifact acceptance evidence, the
    reference is also represented as a first-class COMPUTE_RESULT Artifact so
    queued dependents can receive the declared logical artifact pointer.
    """
    reference = (result_reference or "").strip()
    if not reference:
        return None
    job = await get_compute_job_for_task(session, job_id=job_id, task_id=task_id)
    if job is None:
        return None
    task = await session.get(Task, task_id)
    logical_key = downstream_artifact_logical_key(task)
    job.result_reference = reference
    artifact = None
    if job.artifact_id is not None:
        artifact = await session.get(Artifact, job.artifact_id)
        if artifact is not None and logical_key:
            artifact.logical_key = logical_key
            if not artifact.storage_uri:
                artifact.storage_uri = reference
            session.add(artifact)
    elif logical_key:
        artifact = Artifact(
            task_id=task_id,
            artifact_type=ArtifactType.COMPUTE_RESULT,
            storage_uri=reference,
            logical_key=logical_key,
            logs=json.dumps(
                {
                    "compute_job_id": str(job.id),
                    "result_reference": reference,
                },
                sort_keys=True,
            ),
        )
        session.add(artifact)
        await session.flush()
        job.artifact_id = artifact.id
    if task is not None and artifact is not None:
        await deliver_artifact_to_dependents(task, artifact, session)
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
    # An operator-killed job may still have written durable artifacts before
    # teardown — link them as a first-class result (best-effort, see helper).
    await _link_terminal_artifact(session, job)
    # Fire the compute-job-terminal wake so any PARKED task waiting on this
    # job wakes immediately rather than sitting until its park deadline.
    # Mirrors the monitor path (see _process_single_compute_job).
    if job.task_id is not None:
        from app.task_lifecycle import fire_task_wakes

        await fire_task_wakes(
            session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job_id),
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
                | (
                    ComputeJob.completed_at.is_not(None)
                    & (ComputeJob.completed_at >= cutoff)
                )
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
        try:
            await _process_single_compute_job(job, session_factory=session_factory)
        except Exception as exc:
            logger.exception(
                "[COMPUTE_JOB] monitor failed for job=%s: %s",
                job.id,
                exc,
            )


async def compute_job_monitor_service(session_factory=AsyncSessionLocal) -> None:
    """Leader-only monitor: launch pending jobs, accrue cost, enforce caps."""
    while True:
        await process_compute_jobs_once(session_factory=session_factory)
        await asyncio.sleep(COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)


async def _process_single_compute_job(
    job: ComputeJob, *, session_factory=AsyncSessionLocal
) -> None:
    now = _utcnow()
    backend = get_compute_backend(job.backend)

    task = None
    if job.task_id:
        async with session_factory() as session:
            task = await session.get(Task, job.task_id)

    if job.task_id is not None and (
        task is None or task.status in _TASK_TERMINAL_STATUSES
    ):
        terminated = True
        if job.external_compute_id:
            terminated = await backend.terminate(job, reason="task_terminal")
        await _apply_task_terminal_cancellation(
            session_factory,
            job.id,
            now=now,
            terminate_ok=terminated,
        )
        return

    if job.status in _COMPUTE_JOB_PRELAUNCH_STATUSES:
        reserved = await _reserve_compute_job_launch(
            session_factory, job.id, backend, now
        )
        if reserved is None:
            return
        result = await backend.launch(_build_launch_spec(reserved))
        await _apply_launch_result(
            session_factory,
            job.id,
            result,
            now=now,
        )
        return

    # ── Running: poll STATUS first, then cost. A job that finished just before
    # this tick must be billed through finished_at, not through poll time, so
    # spend-cap decisions are never made from an inflated accrual. ──
    status = await backend.poll_status(job)
    poll_attempt_count = (job.poll_attempt_count or 0) + 1
    next_poll_at = now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)

    if status.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        finished = status.finished_at or now
        cost = await backend.poll_cost(job, as_of=finished)
        accrued_cost_usd = max(
            float(job.accrued_cost_usd or 0.0), cost.total_cost_usd
        )
        outcome = (
            ComputeJobOutcome.SUCCESS
            if status.status == ComputeJobStatus.SUCCEEDED
            else ComputeJobOutcome.FAILURE
        )
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=finished,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=status,
            accrued_cost_usd=accrued_cost_usd,
            terminal_status=status.status,
            outcome=outcome,
            terminal_reason=status.message
            or _meta_message(job)
            or "compute job finished",
            exit_code=status.exit_code,
        )
        return

    # Still running: accrue cost up to now, then enforce deadline/budget.
    cost = await backend.poll_cost(job, as_of=now)
    accrued_cost_usd = max(float(job.accrued_cost_usd or 0.0), cost.total_cost_usd)

    if job.wall_clock_deadline is not None and job.wall_clock_deadline <= now:
        terminated = await backend.terminate(job, reason="deadline")
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=now,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=status,
            accrued_cost_usd=accrued_cost_usd,
            terminal_status=ComputeJobStatus.TIMED_OUT,
            outcome=ComputeJobOutcome.TIMED_OUT,
            terminal_reason="compute job terminated at deadline",
            terminate_ok=terminated,
        )
        return

    if job.spend_cap_usd is not None and accrued_cost_usd >= float(job.spend_cap_usd):
        terminated = await backend.terminate(job, reason="budget")
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=now,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=status,
            accrued_cost_usd=accrued_cost_usd,
            terminal_status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.SPEND_CAP_EXCEEDED,
            terminal_reason="compute job terminated at spend cap",
            terminate_ok=terminated,
        )
        return

    await _apply_polled_compute_job_result(
        session_factory,
        job.id,
        now=now,
        poll_attempt_count=poll_attempt_count,
        next_poll_at=next_poll_at,
        status=status,
        accrued_cost_usd=accrued_cost_usd,
    )


async def _reserve_compute_job_launch(
    session_factory,
    job_id: uuid.UUID,
    backend,
    now,
) -> ComputeJob | None:
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if live is None or live.status not in _COMPUTE_JOB_PRELAUNCH_STATUSES:
            return None
        if live.status == ComputeJobStatus.QUEUED:
            live.status = ComputeJobStatus.STARTING
            if not live.external_compute_id:
                live.external_compute_id = backend.external_id_for(live)
        elif not live.external_compute_id:
            live.external_compute_id = backend.external_id_for(live)
        live.updated_at = now
        await session.commit()
        await session.refresh(live)
        return live


async def _apply_launch_result(
    session_factory,
    job_id: uuid.UUID,
    result,
    *,
    now,
) -> bool:
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if live is None or live.status not in _COMPUTE_JOB_PRELAUNCH_STATUSES:
            return False
        if result.external_id and not live.external_compute_id:
            live.external_compute_id = result.external_id
        live.started_at = live.started_at or result.started_at
        live.status = ComputeJobStatus.RUNNING
        live.last_polled_at = now
        live.next_poll_at = now + timedelta(
            seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS
        )
        extra: dict[str, Any] = dict(result.metadata or {})
        if result.cost_rate_per_second and result.cost_rate_per_second > 0:
            extra["cost_rate_per_second"] = result.cost_rate_per_second
        extra["status_message"] = result.message or "compute job launched"
        _set_meta(live, **extra)
        await session.commit()
        return True


async def _apply_polled_compute_job_result(
    session_factory,
    job_id: uuid.UUID,
    *,
    now,
    poll_attempt_count: int,
    next_poll_at: datetime,
    status,
    accrued_cost_usd: float,
    terminal_status: ComputeJobStatus | None = None,
    outcome: ComputeJobOutcome | None = None,
    terminal_reason: str | None = None,
    exit_code: int | None = None,
    terminate_ok: bool = True,
) -> bool:
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            return False
        live.poll_attempt_count = poll_attempt_count
        live.last_polled_at = now
        live.next_poll_at = next_poll_at
        live.accrued_cost_usd = accrued_cost_usd
        # Persist any command-reported result metrics under a dedicated
        # polling_metadata key. polling_metadata already serializes to the agent
        # via serialize_compute_job_response, so the metrics reach the handler
        # without a migration. Absent metrics (None) leave the key unwritten.
        if status.metrics:
            _set_meta(live, result_metrics=dict(status.metrics))
        if status.command_log:
            _set_meta(live, command_log=dict(status.command_log))
        if terminal_status is not None and outcome is not None:
            _finalize_terminal_job(
                live,
                status=terminal_status,
                outcome=outcome,
                terminal_reason=terminal_reason or "compute job finished",
                now=now,
                exit_code=exit_code,
                terminate_ok=terminate_ok,
            )
            await _link_terminal_artifact(session, live)
            if live.task_id is not None:
                from app.task_lifecycle import fire_task_wakes

                await fire_task_wakes(
                    session,
                    wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                    wake_source_ref=str(live.id),
                )
        else:
            live.status = ComputeJobStatus.RUNNING
            if status.message:
                _set_meta(live, status_message=status.message)
        await session.commit()
        return True


async def _apply_task_terminal_cancellation(
    session_factory,
    job_id: uuid.UUID,
    *,
    now,
    terminate_ok: bool,
) -> bool:
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            return False
        live_task = await session.get(Task, live.task_id) if live.task_id else None
        if live.task_id is not None and (
            live_task is None or live_task.status not in _TASK_TERMINAL_STATUSES
        ):
            return False
        _finalize_terminal_job(
            live,
            status=ComputeJobStatus.CANCELLED,
            outcome=ComputeJobOutcome.CANCELLED,
            terminal_reason="owning task is terminal; compute job cancelled",
            now=now,
            terminate_ok=terminate_ok,
        )
        await _link_terminal_artifact(session, live)
        if live.task_id is not None:
            from app.task_lifecycle import fire_task_wakes

            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(live.id),
            )
        await session.commit()
        return True


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
        accelerator=job.accelerator_type,
        accelerator_count=int(job.accelerator_count or 1),
    )


def _meta_message(job: ComputeJob) -> str | None:
    return (job.polling_metadata or {}).get("status_message")


def _set_meta(
    job: ComputeJob, *, status_message: str | None = None, **extra: Any
) -> None:
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


def _coerce_metric_number(value: Any) -> float | int | None:
    """Numeric coercion mirroring the agent's ``numOrNull`` for reported metrics.

    Accepts finite ints/floats and numeric strings; rejects bools, NaN/inf, and
    non-numeric values so a reported metric can only land as a real number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str) and value.strip():
        try:
            num = float(value)
        except ValueError:
            return None
        return num if num == num and num not in (float("inf"), float("-inf")) else None
    return None


def build_compute_result_logs(job: ComputeJob) -> str:
    """Serialize a terminal :class:`ComputeJob` into the COMPUTE_RESULT artifact.

    This is the backend-side equivalent of the JSON artifact the agent's
    compute-experiment handler used to emit after polling the job to terminal: a
    JSON object carrying the terminal ``outcome`` plus
    ``final_cost_usd``/``accrued_cost_usd`` (and any command-reported experiment
    metrics flattened to top-level numeric keys) that
    ``_handle_compute_experiment_completion`` gates and persists. Now that a
    compute_experiment task PARKS on its job instead of poll-pinning a VM, the
    leader-owned wake worker builds this artifact straight from the job row so the
    parked task is finalized with the honest terminal outcome, final cost, and
    result reference — no agent monitoring required. Keeps the artifact shape in
    one place, in sync with the agent's ``RESERVED_RESULT_KEYS``.
    """
    meta = job.polling_metadata or {}
    # status/outcome may surface as the str-enum member OR a bare string depending
    # on how the row was loaded; normalize to the string value either way.
    outcome = getattr(job.outcome, "value", job.outcome)
    status_value = getattr(job.status, "value", job.status)
    # Coerce the normalized string outcome to the enum so the lookup works
    # whether the ORM loaded the column as a ComputeJobOutcome enum instance or
    # as a raw string (e.g. "SPEND_CAP_EXCEEDED").
    try:
        outcome_enum = ComputeJobOutcome(outcome) if outcome is not None else None
    except ValueError:
        outcome_enum = None
    intentional, killed_by = (
        _COMPUTE_OUTCOME_DISPOSITION.get(outcome_enum, (False, None))
        if outcome_enum is not None
        else (False, None)
    )
    final_cost = job.final_cost_usd
    if final_cost is None:
        final_cost = job.accrued_cost_usd
    result: dict[str, Any] = {
        "job_id": str(job.id),
        "backend": job.backend,
        "machine_type": job.machine_type,
        "status": status_value,
        # Prefer the honest outcome; fall back to the terminal status so the
        # downstream gate still sees a non-empty outcome on a legacy row.
        "outcome": outcome or status_value,
        "intentional_terminal": intentional,
        "killed_by": killed_by,
        "final_cost_usd": final_cost,
        "accrued_cost_usd": job.accrued_cost_usd,
        "spend_cap_usd": job.spend_cap_usd,
        "terminal_reason": job.terminal_reason,
        "result_reference": job.result_reference,
    }
    # Flatten command-reported result metrics to top-level numeric keys so the
    # success-criterion gate can read them by name, exactly as the agent did. A
    # reported metric whose name collides with a job-level key is skipped so it
    # can never overwrite the reserved field.
    reported = meta.get("result_metrics")
    if isinstance(reported, dict):
        for name, value in reported.items():
            if name in result:
                continue
            num = _coerce_metric_number(value)
            if num is not None:
                result[name] = num
    return json.dumps(result)


async def _link_terminal_artifact(session, job: ComputeJob) -> None:
    """Turn a terminal job's durable artifact location into a first-class result.

    When a terminal compute job produced durable, non-metric output (checkpoints,
    plots, logs) that survived VM teardown, create one ``Artifact`` row for that
    stored result and stamp ``ComputeJob.artifact_id`` + ``ComputeJob.result_reference``
    to point at it — reusing the existing artifact/compute-job columns rather than
    a parallel persistence channel. The agent's terminal COMPUTE_RESULT payload
    then forwards the durable reference it reads back off the row.

    Best-effort and idempotent:
      * skips a job with no owning task or one already linked (re-entry safe);
      * only a location with at least one durable object becomes an Artifact —
        an empty artifact directory degrades to 'no artifact';
      * the Artifact INSERT runs inside a SAVEPOINT so a failure (e.g. the owning
        task was concurrently deleted) rolls back ONLY the linkage, never the
        job's terminal completion, and is logged rather than raised.
    """
    if job.task_id is None or job.artifact_id is not None:
        return
    reference = await collect_terminal_artifact_reference(job)
    if reference is None:
        return
    task = await session.get(Task, job.task_id)
    try:
        async with session.begin_nested():
            artifact_logs = {
                "compute_job_id": str(job.id),
                "backend": reference.backend,
                "artifact_uri": reference.uri,
                "file_count": reference.file_count,
                "outcome": (
                    job.outcome.value if job.outcome is not None else None
                ),
                "final_cost_usd": job.final_cost_usd,
            }
            command_log = (job.polling_metadata or {}).get("command_log")
            if command_log is not None:
                artifact_logs["command_log"] = command_log
            artifact = Artifact(
                task_id=job.task_id,
                artifact_type=ArtifactType.COMPUTE_RESULT,
                storage_uri=reference.uri,
                logs=json.dumps(artifact_logs, sort_keys=True),
            )
            stamp_artifact_logical_key(artifact, task)
            session.add(artifact)
            await session.flush()  # assign artifact.id before stamping the job
        # SAVEPOINT released cleanly — safe to point the job at the durable result.
        job.artifact_id = artifact.id
        job.result_reference = reference.uri
        job.updated_at = _utcnow()
        session.add(job)
        if task is not None:
            await deliver_artifact_to_dependents(task, artifact, session)
    except Exception:
        logger.warning(
            "[COMPUTE_JOB] durable-artifact linkage failed for job=%s; "
            "completing without an artifact link",
            job.id,
            exc_info=True,
        )
