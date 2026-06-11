"""Persistence and monitoring for external compute jobs.

The persisted :class:`app.models.ComputeJob` row is the single source of truth.
Substrate-specific runtime data the schema has no dedicated column for (launch
command spec, provisioning model, per-second cost rate, GCP zone, exit code)
lives in ``ComputeJob.polling_metadata``; terminal/outcome state uses the
upstream ``status`` / ``outcome`` / ``terminal_reason`` columns.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.compute_backends import (
    LOCAL_BACKEND_NAME,
    ComputeLaunchSpec,
    ComputeStatusResult,
    collect_terminal_artifact_reference,
    default_compute_backend_name,
    get_compute_backend,
    validate_backend_name,
)
from app.release_artifacts import (
    summarize_release_artifacts,
    summarize_required_artifact_gap,
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
    Project,
    ReleaseVersion,
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
_COMPUTE_RPC_DIAGNOSTIC_KEY = "compute_rpc_diagnostic"
_COMPUTE_RPC_DIAGNOSTIC_MAX_STRING = 1000

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
_LIVE_PROOF_METADATA_KEY = "live_release_evidence"
_LIVE_PROOF_MARKER_KEYS = (
    "live_proof",
    "live_proof_marker",
    "live_release_proof",
    "live_release_proof_marker",
    "requires_live_release",
)

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


class ComputeLaunchPreconditionFailed(Exception):
    """Structured launch precondition failure safe to return to agents."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("message") or detail.get("error") or detail))


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
    live_proof: Any | None = None,
    live_proof_marker: Any | None = None,
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
    locked_task = await _lock_task_for_compute_job(session, task.id)
    if locked_task is None:
        raise ValueError("task not found")
    if locked_task.status in _TASK_TERMINAL_STATUSES:
        raise ValueError("cannot launch a compute job for a terminal task")
    live_proof_requirement = _resolve_live_proof_requirement(
        locked_task,
        explicit_values=(live_proof_marker, live_proof),
    )
    live_release_evidence = None
    if live_proof_requirement is not None:
        live_release_evidence = await _resolve_live_release_evidence(
            session,
            project_id=locked_task.project_id,
            requirement=live_proof_requirement,
        )
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
        live_proof_requirement=live_proof_requirement,
    )
    existing = await _get_active_compute_job_for_task(session, task_id=locked_task.id)
    if existing is not None:
        if _launch_request_matches(existing, launch_request_fingerprint):
            if live_release_evidence is not None:
                metadata = dict(existing.polling_metadata or {})
                metadata[_LIVE_PROOF_METADATA_KEY] = live_release_evidence
                existing.polling_metadata = metadata
                existing.updated_at = _utcnow()
                session.add(existing)
                await session.commit()
                await session.refresh(existing)
            return existing
        raise ValueError("task already has an active compute job")
    await _check_project_compute_job_concurrency_cap(
        session,
        project_id=locked_task.project_id,
    )
    await _check_project_spend_ceiling(
        session,
        project_id=locked_task.project_id,
        requested_cap_usd=capped_spend,
    )
    deadline_at = _utcnow() + timedelta(seconds=capped_deadline)
    # Accelerator-aware rate: base machine + GPU surcharge (N1+T4/V100) or the
    # bundled G2 rate (L4); falls through to the plain VM rate for CPU jobs.
    rate = get_compute_rate_per_second(
        machine_type=resolved_machine_type,
        accelerator=persisted_accelerator,
        count=accelerator_count,
        spot=normalized_provisioning == "SPOT",
    )
    polling_metadata = {
        "command": command,
        "cwd": cwd,
        "env": {str(k): str(v) for k, v in (env or {}).items()},
        "provisioning_model": normalized_provisioning,
        "cost_rate_per_second": rate,
        "requested_deadline_seconds": capped_deadline,
        "launch_request_fingerprint": launch_request_fingerprint,
        "status_message": "queued for compute-job launch",
    }
    if live_release_evidence is not None:
        polling_metadata[_LIVE_PROOF_METADATA_KEY] = live_release_evidence
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
            **polling_metadata,
            "provisioning_deadline_at": deadline_at.isoformat(),
            "runtime_deadline_seconds": capped_deadline,
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def _marker_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "live_proof" if value else None
    if isinstance(value, str):
        marker = value.strip()
        if not marker or marker.lower() in {"0", "false", "no", "off", "none"}:
            return None
        return marker
    if isinstance(value, dict):
        enabled = value.get("enabled")
        if enabled is False:
            return None
        for key in ("marker", "name", "kind", "target", "reason"):
            marker = _marker_from_value(value.get(key))
            if marker:
                return marker
        return "live_proof"
    return None


def _resolve_live_proof_requirement(
    task: Task,
    *,
    explicit_values: tuple[Any, ...] = (),
) -> dict[str, str] | None:
    for value in explicit_values:
        marker = _marker_from_value(value)
        if marker:
            return {"marker": marker}

    containers: list[dict[str, Any]] = []
    payload = task.first_payload or {}
    context = task.context or {}
    if isinstance(payload, dict):
        containers.append(payload)
        for key in ("compute_experiment", "launch_contract"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
    if isinstance(context, dict):
        containers.append(context)
        for key in ("compute_experiment", "launch_contract"):
            nested = context.get(key)
            if isinstance(nested, dict):
                containers.append(nested)

    for container in containers:
        for key in _LIVE_PROOF_MARKER_KEYS:
            marker = _marker_from_value(container.get(key))
            if marker:
                return {"marker": marker}
    return None


async def _resolve_live_release_evidence(
    session,
    *,
    project_id: uuid.UUID,
    requirement: dict[str, str],
) -> dict[str, Any]:
    result = await session.execute(
        select(ReleaseVersion)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.status == "promoted",
        )
        .options(selectinload(ReleaseVersion.deploy_artifacts))
        .order_by(ReleaseVersion.promoted_at.desc(), ReleaseVersion.version_number.desc())
    )
    version = result.scalars().first()
    if version is None:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "live_release_precondition_failed",
                "reason": "no_promoted_release",
                "message": "live-proof compute launch requires a promoted release",
                "live_proof": requirement,
            }
        )
    project = await session.get(Project, project_id)
    aggregate, artifacts = summarize_release_artifacts(
        version,
        version.deploy_artifacts,
        project=project,
    )
    required_artifacts = [
        artifact for artifact in artifacts if artifact.get("required")
    ]
    # Skip vacuous (serving_status None) required rows — an absent/legacy
    # provider row is NA, not pending (mirrors summarize_required_artifact_gap
    # and the aggregate projection, which exclude None-status evidence). A
    # genuinely pending row carries a real status ("unconfirmed"/"deploy_failed"),
    # not None, so it still blocks. Require at least one non-vacuous required
    # artifact confirmed so a fully un-armed version never passes as "serving".
    _observed_required = [
        artifact
        for artifact in required_artifacts
        if artifact.get("serving_status") is not None
    ]
    required_artifacts_confirmed = bool(_observed_required) and all(
        artifact.get("serving_status") == "serving_confirmed"
        for artifact in _observed_required
    )
    evidence = {
        "live_proof": requirement,
        "release_version_id": str(version.id),
        "release_version_number": version.version_number,
        "promoted_sha": version.promoted_sha,
        "promoted_at": version.promoted_at.isoformat()
        if version.promoted_at
        else None,
        "aggregate_serving_status": aggregate.get("serving_status"),
        "aggregate_reason_code": aggregate.get("reason_code"),
        "required_artifacts": required_artifacts,
    }
    if not required_artifacts_confirmed:
        gap = summarize_required_artifact_gap(required_artifacts)
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "live_release_precondition_failed",
                "reason": "release_not_serving_confirmed",
                "message": (
                    gap
                    or "live-proof compute launch requires all required artifacts serving_confirmed"
                ),
                "live_proof": requirement,
                "live_release_evidence": evidence,
            }
        )
    return evidence


async def _check_project_spend_ceiling(
    session,
    *,
    project_id: uuid.UUID,
    requested_cap_usd: float,
) -> None:
    """Raise ComputeLaunchPreconditionFailed when launching would breach the project ceiling.

    Computes total 24h spend as: settled terminal-job costs + in-flight accrued
    compute costs + agent-VM/LLM task spend (via compute_project_window_spend_usd).
    If that total plus the requested cap would exceed the configured
    ``daily_spend_ceiling_usd``, the launch is rejected before any row is written.
    The check is skipped when no ceiling is configured.
    """
    project = await session.get(Project, project_id)
    ceiling_usd = (project.build_config or {}).get("daily_spend_ceiling_usd") if project else None
    if not ceiling_usd:
        return
    ceiling_usd = float(ceiling_usd)
    since = _utcnow() - timedelta(hours=24)
    settled = await get_recent_terminal_compute_spend_usd(
        session, project_id=project_id, since=since
    )
    inflight_result = await session.execute(
        select(func.coalesce(func.sum(ComputeJob.accrued_cost_usd), 0.0)).where(
            ComputeJob.project_id == project_id,
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES),
        )
    )
    inflight = float(inflight_result.scalar_one() or 0.0)
    compute_rolling = settled + inflight
    # Add agent-VM + LLM spend to get total cross-lane project spend. This helper
    # covers Task-based costs only and does NOT include ComputeJob spend, so there
    # is no double-counting with compute_rolling above.
    from app.efficiency_snapshot import compute_project_window_spend_usd

    task_spend = await compute_project_window_spend_usd(session, project_id)
    rolling = compute_rolling + task_spend
    if rolling + requested_cap_usd > ceiling_usd:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "project_spend_ceiling_exceeded",
                "message": (
                    f"project rolling 24h total spend ${rolling:.4f} (compute: "
                    f"${compute_rolling:.4f}, agent VM+LLM: ${task_spend:.4f}) plus "
                    f"requested cap ${requested_cap_usd:.4f} would exceed configured "
                    f"ceiling ${ceiling_usd:.4f}"
                ),
                "rolling_spend_usd": rolling,
                "compute_spend_usd": compute_rolling,
                "task_spend_usd": task_spend,
                "requested_cap_usd": requested_cap_usd,
                "ceiling_usd": ceiling_usd,
            }
        )


async def _check_project_compute_job_concurrency_cap(
    session,
    *,
    project_id: uuid.UUID,
) -> None:
    """Raise ComputeLaunchPreconditionFailed when the project is at its compute-job cap.

    Counts active ComputeJob rows for the project and rejects a new launch when
    the count is at or above the configured cap. The check is skipped when no cap
    is configured for the project (absent or zero in
    ``max_concurrent_compute_jobs_by_project``).
    """
    from app.runtime_config_state import resolve_project_compute_job_concurrency_cap

    cap = resolve_project_compute_job_concurrency_cap(project_id)
    if cap is None:
        return
    count_result = await session.execute(
        select(func.count()).where(
            ComputeJob.project_id == project_id,
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES),
        )
    )
    active_count = int(count_result.scalar_one() or 0)
    if active_count >= cap:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "compute_job_concurrency_limit_exceeded",
                "message": (
                    f"project already has {active_count} active compute job(s); "
                    f"configured cap is {cap}"
                ),
                "active_count": active_count,
                "cap": cap,
            }
        )


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
    live_proof_requirement: dict[str, str] | None = None,
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
        "live_proof_requirement": live_proof_requirement,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _launch_request_matches(job: ComputeJob, request_fingerprint: str) -> bool:
    existing = str(
        (job.polling_metadata or {}).get("launch_request_fingerprint") or ""
    ).strip()
    return bool(existing) and existing == request_fingerprint


def _requested_deadline_seconds(job: ComputeJob) -> int | None:
    raw = (job.polling_metadata or {}).get("requested_deadline_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_naive_iso_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    return None


def _runtime_deadline_at(job: ComputeJob) -> datetime | None:
    meta_deadline = _parse_naive_iso_datetime(
        (job.polling_metadata or {}).get("runtime_deadline_at")
    )
    if meta_deadline is not None:
        return meta_deadline
    if job.started_at is None:
        return None
    requested = _requested_deadline_seconds(job)
    if requested is None:
        return None
    return job.started_at + timedelta(seconds=requested)


def _provisioning_timeout_seconds(job: ComputeJob, *, now: datetime) -> float | None:
    if job.wall_clock_deadline is None:
        return None
    return max(0.0, (job.wall_clock_deadline - now).total_seconds())


def _status_is_runtime_timeout(status: ComputeStatusResult) -> bool:
    message = (status.message or "").strip().lower()
    if message in {"runtime_ttl", "runtime_timeout", "command_runtime_timeout"}:
        return True
    return status.exit_code == 124


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
            if not artifact.storage_uri and _is_durable_artifact_uri(reference):
                artifact.storage_uri = reference
            session.add(artifact)
    elif logical_key and _is_durable_artifact_uri(reference):
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
) -> ComputeJob | None:
    """Operator-initiated kill of an active compute job.

    Validates the job exists and is non-terminal (under lock), releases the lock,
    terminates the external compute via the backend (lock-free RPC), then
    re-acquires the lock to drive the job to terminal state. The job is driven to
    the same terminal shape the monitor produces (CANCELLED status + CANCELLED
    outcome) so the operator kill is distinguishable from a budget
    (SPEND_CAP_EXCEEDED) or deadline (TIMED_OUT) teardown by outcome, and from a
    task-terminal cancel by the reason text. A peer that drove the job terminal
    during the lock-free window degrades gracefully to no-op. Rejects an
    already-terminal job.
    """
    job = await _lock_compute_job(session, job_id)
    if job is None or job.project_id != project_id:
        raise ValueError("compute job not found")
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        raise ValueError("compute job is already terminal")
    backend_name = job.backend
    external_compute_id = job.external_compute_id
    await session.commit()

    now = _utcnow()
    backend = get_compute_backend(backend_name)
    terminated = True
    if external_compute_id:
        started_at = _utcnow()
        try:
            terminated = await backend.terminate(job, reason="operator")
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] operator terminate failed for job=%s: %s",
                job_id,
                exc,
            )
            return await _persist_compute_rpc_failure(
                session,
                job_id=job_id,
                operation="terminate",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=None,
                reason="operator",
            )

    by = f" ({killed_by})" if killed_by else ""
    return await _apply_operator_kill_result(
        session,
        job_id=job_id,
        status=ComputeJobStatus.CANCELLED,
        outcome=ComputeJobOutcome.CANCELLED,
        terminal_reason=f"killed by operator{by}",
        now=now,
        terminate_ok=terminated,
    )


async def _apply_operator_kill_result(
    session,
    *,
    job_id: uuid.UUID,
    status: ComputeJobStatus,
    outcome: ComputeJobOutcome,
    terminal_reason: str,
    now,
    terminate_ok: bool = True,
) -> ComputeJob | None:
    """Apply operator-kill terminal state after lock-free backend RPC.

    Re-acquires the job lock, re-checks the job is still non-terminal (gracefully
    returns None if a peer made it terminal during the RPC), applies the terminal
    state, links durable artifacts, and fires the compute-job-terminal wake.
    Mirrors the pattern used by _apply_polled_compute_job_result in the monitor.
    """
    live = await _lock_compute_job(session, job_id)
    if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        return None
    _finalize_terminal_job(
        live,
        status=status,
        outcome=outcome,
        terminal_reason=terminal_reason,
        now=now,
        terminate_ok=terminate_ok,
    )
    await _link_terminal_artifact(session, live)
    if live.task_id is not None:
        from app.task_lifecycle import fire_task_wakes

        await fire_task_wakes(
            session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job_id),
        )
    await session.commit()
    await session.refresh(live)
    return live


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


async def get_active_compute_job_accrued_spend_usd(
    session,
    *,
    project_id: uuid.UUID,
) -> float:
    """Sum ``accrued_cost_usd`` over active (non-terminal) compute jobs for a project.

    In-flight jobs accumulate real spend before reaching a terminal status and
    recording ``final_cost_usd``.  Including them closes the gap between
    money-spent and money-reported in the daily cost ceiling check.  No date
    filter: a job that started yesterday is still spending today, so its full
    accrued cost belongs in today's ceiling total.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(ComputeJob.accrued_cost_usd), 0.0)).where(
            ComputeJob.project_id == project_id,
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES),
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
        except Exception as exc:  # noqa: PERF203
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
        terminate_diagnostic = None
        if job.external_compute_id:
            started_at = _utcnow()
            try:
                terminated = await backend.terminate(job, reason="task_terminal")
            except Exception as exc:
                failed_at = _utcnow()
                logger.exception(
                    "[COMPUTE_JOB] task-terminal terminate failed for job=%s: %s",
                    job.id,
                    exc,
                )
                terminated = False
                terminate_diagnostic = _build_compute_rpc_diagnostic(
                    job,
                    operation="terminate",
                    exc=exc,
                    started_at=started_at,
                    failed_at=failed_at,
                    next_retry_at=None,
                    reason="task_terminal",
                )
        await _apply_task_terminal_cancellation(
            session_factory,
            job.id,
            now=now,
            terminate_ok=terminated,
            compute_rpc_diagnostic=terminate_diagnostic,
        )
        return

    if job.status in _COMPUTE_JOB_PRELAUNCH_STATUSES:
        reserved = await _reserve_compute_job_launch(
            session_factory, job.id, backend, now
        )
        if reserved is None:
            return
        provisioning_timeout_seconds = _provisioning_timeout_seconds(job, now=now)
        if provisioning_timeout_seconds == 0.0:
            started_at = _utcnow()
            terminate_diagnostic = None
            try:
                terminated = await backend.terminate(reserved, reason="provisioning")
            except Exception as exc:
                failed_at = _utcnow()
                logger.exception(
                    "[COMPUTE_JOB] provisioning deadline terminate failed for job=%s: %s",
                    job.id,
                    exc,
                )
                terminated = False
                terminate_diagnostic = _build_compute_rpc_diagnostic(
                    job,
                    operation="terminate",
                    exc=exc,
                    started_at=started_at,
                    failed_at=failed_at,
                    next_retry_at=None,
                    reason="provisioning",
                )
            await _apply_polled_compute_job_result(
                session_factory,
                job.id,
                now=now,
                poll_attempt_count=(job.poll_attempt_count or 0) + 1,
                next_poll_at=now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
                status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
                accrued_cost_usd=float(job.accrued_cost_usd or 0.0),
                terminal_status=ComputeJobStatus.TIMED_OUT,
                outcome=ComputeJobOutcome.TIMED_OUT,
                terminal_reason="compute job timed out while provisioning",
                terminate_ok=terminated,
                compute_rpc_diagnostic=terminate_diagnostic
                or _build_compute_rpc_diagnostic(
                    job,
                    operation="launch",
                    exc=TimeoutError("compute job provisioning deadline exceeded"),
                    started_at=now,
                    failed_at=now,
                    next_retry_at=None,
                    reason="provisioning_timeout",
                ),
            )
            return
        started_at = _utcnow()
        try:
            launch_coro = backend.launch(_build_launch_spec(reserved))
            if provisioning_timeout_seconds is not None:
                result = await asyncio.wait_for(
                    launch_coro, timeout=provisioning_timeout_seconds
                )
            else:
                result = await launch_coro
        except asyncio.TimeoutError:
            failed_at = _utcnow()
            logger.warning(
                "[COMPUTE_JOB] launch provisioning timed out for job=%s", job.id
            )
            terminate_diagnostic = None
            try:
                terminated = await backend.terminate(reserved, reason="provisioning")
            except Exception as exc:
                failed_at = _utcnow()
                logger.exception(
                    "[COMPUTE_JOB] provisioning deadline terminate failed for job=%s: %s",
                    job.id,
                    exc,
                )
                terminated = False
                terminate_diagnostic = _build_compute_rpc_diagnostic(
                    job,
                    operation="terminate",
                    exc=exc,
                    started_at=started_at,
                    failed_at=failed_at,
                    next_retry_at=None,
                    reason="provisioning",
                )
            await _apply_polled_compute_job_result(
                session_factory,
                job.id,
                now=now,
                poll_attempt_count=(job.poll_attempt_count or 0) + 1,
                next_poll_at=now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
                status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
                accrued_cost_usd=float(job.accrued_cost_usd or 0.0),
                terminal_status=ComputeJobStatus.TIMED_OUT,
                outcome=ComputeJobOutcome.TIMED_OUT,
                terminal_reason="compute job timed out while provisioning",
                terminate_ok=terminated,
                compute_rpc_diagnostic=terminate_diagnostic
                or _build_compute_rpc_diagnostic(
                    job,
                    operation="launch",
                    exc=TimeoutError("compute job provisioning deadline exceeded"),
                    started_at=started_at,
                    failed_at=failed_at,
                    next_retry_at=None,
                    reason="provisioning_timeout",
                ),
            )
            return
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] launch failed for job=%s: %s",
                job.id,
                exc,
            )
            await _record_compute_rpc_failure(
                session_factory,
                job.id,
                operation="launch",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=failed_at
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
            )
            return
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
    started_at = _utcnow()
    try:
        status = await backend.poll_status(job)
    except Exception as exc:
        failed_at = _utcnow()
        logger.exception(
            "[COMPUTE_JOB] poll_status failed for job=%s: %s",
            job.id,
            exc,
        )
        await _record_compute_rpc_failure(
            session_factory,
            job.id,
            operation="poll_status",
            exc=exc,
            started_at=started_at,
            failed_at=failed_at,
            next_retry_at=failed_at
            + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
        )
        return
    poll_attempt_count = (job.poll_attempt_count or 0) + 1
    next_poll_at = now + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)

    if status.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        finished = status.finished_at or now
        started_at = _utcnow()
        try:
            cost = await backend.poll_cost(job, as_of=finished)
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] poll_cost failed for terminal job=%s: %s",
                job.id,
                exc,
            )
            await _record_compute_rpc_failure(
                session_factory,
                job.id,
                operation="poll_cost",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=failed_at
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
            )
            return
        accrued_cost_usd = max(
            float(job.accrued_cost_usd or 0.0), cost.total_cost_usd
        )
        timeout_like = _status_is_runtime_timeout(status)
        outcome = (
            ComputeJobOutcome.TIMED_OUT
            if timeout_like
            else (
                ComputeJobOutcome.SUCCESS
                if status.status == ComputeJobStatus.SUCCEEDED
                else ComputeJobOutcome.FAILURE
            )
        )
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=finished,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=status,
            accrued_cost_usd=accrued_cost_usd,
            terminal_status=ComputeJobStatus.TIMED_OUT
            if timeout_like
            else status.status,
            outcome=outcome,
            terminal_reason=(
                "compute job exceeded runtime deadline"
                if timeout_like
                else status.message or _meta_message(job) or "compute job finished"
            ),
            exit_code=status.exit_code,
        )
        return

    # Still running: accrue cost up to now, then enforce deadline/budget.
    started_at = _utcnow()
    try:
        cost = await backend.poll_cost(job, as_of=now)
    except Exception as exc:
        failed_at = _utcnow()
        logger.exception(
            "[COMPUTE_JOB] poll_cost failed for running job=%s: %s",
            job.id,
            exc,
        )
        await _record_compute_rpc_failure(
            session_factory,
            job.id,
            operation="poll_cost",
            exc=exc,
            started_at=started_at,
            failed_at=failed_at,
            next_retry_at=failed_at
            + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
        )
        return
    accrued_cost_usd = max(float(job.accrued_cost_usd or 0.0), cost.total_cost_usd)

    runtime_deadline_at = _runtime_deadline_at(job)
    if runtime_deadline_at is not None and runtime_deadline_at <= now:
        started_at = _utcnow()
        terminate_diagnostic = None
        try:
            terminated = await backend.terminate(job, reason="deadline")
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] runtime deadline terminate failed for job=%s: %s",
                job.id,
                exc,
            )
            terminated = False
            terminate_diagnostic = _build_compute_rpc_diagnostic(
                job,
                operation="terminate",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=None,
                reason="deadline",
            )
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
            terminal_reason="compute job exceeded runtime deadline",
            terminate_ok=terminated,
            compute_rpc_diagnostic=terminate_diagnostic,
        )
        return

    if job.spend_cap_usd is not None and accrued_cost_usd >= float(job.spend_cap_usd):
        started_at = _utcnow()
        terminate_diagnostic = None
        try:
            terminated = await backend.terminate(job, reason="budget")
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] budget terminate failed for job=%s: %s",
                job.id,
                exc,
            )
            terminated = False
            terminate_diagnostic = _build_compute_rpc_diagnostic(
                job,
                operation="terminate",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=None,
                reason="budget",
            )
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
            compute_rpc_diagnostic=terminate_diagnostic,
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
        runtime_seconds = _requested_deadline_seconds(live)
        if runtime_seconds is not None:
            extra.setdefault("runtime_deadline_seconds", runtime_seconds)
            extra["runtime_deadline_at"] = (
                (live.started_at or result.started_at)
                + timedelta(seconds=runtime_seconds)
            ).isoformat()
        if result.cost_rate_per_second and result.cost_rate_per_second > 0:
            extra["cost_rate_per_second"] = result.cost_rate_per_second
        extra["status_message"] = result.message or "compute job launched"
        _set_meta(live, **extra)
        _clear_compute_rpc_diagnostic(live)
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
    compute_rpc_diagnostic: dict[str, Any] | None = None,
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
        if compute_rpc_diagnostic is None:
            _clear_compute_rpc_diagnostic(live)
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
            if compute_rpc_diagnostic is not None:
                _set_meta(live, compute_rpc_diagnostic=compute_rpc_diagnostic)
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
    compute_rpc_diagnostic: dict[str, Any] | None = None,
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
        if compute_rpc_diagnostic is not None:
            _set_meta(live, compute_rpc_diagnostic=compute_rpc_diagnostic)
        else:
            _clear_compute_rpc_diagnostic(live)
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
        provisioning_deadline_at=job.wall_clock_deadline,
        runtime_deadline_seconds=int(meta.get("requested_deadline_seconds") or 0)
        or None,
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


def _clear_compute_rpc_diagnostic(job: ComputeJob) -> None:
    meta = dict(job.polling_metadata or {})
    if _COMPUTE_RPC_DIAGNOSTIC_KEY not in meta:
        return
    meta.pop(_COMPUTE_RPC_DIAGNOSTIC_KEY, None)
    job.polling_metadata = meta


def _bounded_rpc_string(value: Any, *, limit: int = _COMPUTE_RPC_DIAGNOSTIC_MAX_STRING) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _rpc_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _build_compute_rpc_diagnostic(
    job: ComputeJob,
    *,
    operation: str,
    exc: Exception,
    started_at: datetime,
    failed_at: datetime,
    next_retry_at: datetime | None,
    reason: str | None = None,
) -> dict[str, Any]:
    meta = job.polling_metadata or {}
    previous = meta.get(_COMPUTE_RPC_DIAGNOSTIC_KEY)
    prior_attempts = 0
    if isinstance(previous, dict) and previous.get("operation") == operation:
        try:
            prior_attempts = int(previous.get("attempt_count") or 0)
        except (TypeError, ValueError):
            prior_attempts = 0
    retry_after_seconds = None
    if next_retry_at is not None:
        retry_after_seconds = max(0.0, (next_retry_at - failed_at).total_seconds())
    diagnostic: dict[str, Any] = {
        "operation": _bounded_rpc_string(operation, limit=64),
        "error_type": _bounded_rpc_string(type(exc).__name__, limit=200),
        "error_message": _bounded_rpc_string(exc),
        "started_at": _rpc_timestamp(started_at),
        "failed_at": _rpc_timestamp(failed_at),
        "duration_ms": max(
            0,
            int((failed_at - started_at).total_seconds() * 1000),
        ),
        "attempt_count": prior_attempts + 1,
        "job_status": getattr(job.status, "value", job.status),
        "next_retry_at": _rpc_timestamp(next_retry_at),
        "retry_after_seconds": retry_after_seconds,
    }
    if reason is not None:
        diagnostic["reason"] = _bounded_rpc_string(reason, limit=200)
    return diagnostic


async def _persist_compute_rpc_failure(
    session,
    *,
    job_id: uuid.UUID,
    operation: str,
    exc: Exception,
    started_at: datetime,
    failed_at: datetime,
    next_retry_at: datetime | None,
    reason: str | None = None,
) -> ComputeJob | None:
    live = await _lock_compute_job(session, job_id)
    if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        return None
    diagnostic = _build_compute_rpc_diagnostic(
        live,
        operation=operation,
        exc=exc,
        started_at=started_at,
        failed_at=failed_at,
        next_retry_at=next_retry_at,
        reason=reason,
    )
    _set_meta(live, compute_rpc_diagnostic=diagnostic)
    live.last_polled_at = failed_at
    live.next_poll_at = next_retry_at
    live.updated_at = failed_at
    session.add(live)
    await session.commit()
    await session.refresh(live)
    return live


async def _record_compute_rpc_failure(
    session_factory,
    job_id: uuid.UUID,
    *,
    operation: str,
    exc: Exception,
    started_at: datetime,
    failed_at: datetime,
    next_retry_at: datetime | None,
    reason: str | None = None,
) -> ComputeJob | None:
    async with session_factory() as session:
        return await _persist_compute_rpc_failure(
            session,
            job_id=job_id,
            operation=operation,
            exc=exc,
            started_at=started_at,
            failed_at=failed_at,
            next_retry_at=next_retry_at,
            reason=reason,
        )


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


def _is_durable_artifact_uri(reference: str) -> bool:
    """True only for references that are durable cloud-storage or file URIs.

    Rejects bare http:// / https:// references (transient signed URLs, arbitrary
    web endpoints) so they cannot create Artifact rows or trigger downstream
    delivery. Accepts gs://, s3://, az://, file: URIs and path-like strings
    containing a ``/`` separator (including metric fragment refs such as
    ``gs://...#metric:name=value``). Empty strings are rejected.
    """
    text = reference.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    if lowered.startswith(("gs://", "s3://", "az://", "file:")):
        return True
    return "/" in text


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
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPUTE_JOB] durable-artifact linkage failed for job=%s; "
            "completing without an artifact link",
            job.id,
            exc_info=True,
        )
