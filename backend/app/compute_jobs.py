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
    GCP_BACKEND_NAME,
    LOCAL_GPU_BACKEND_NAME,
    RUNNER_SUBPROCESS_BACKEND_NAME,
    ComputeLaunchSpec,
    ComputeStatusResult,
    PreflightSmokeResult,
    collect_terminal_artifact_reference,
    default_compute_backend_name,
    detect_local_gpu,
    get_compute_backend,
    is_zero_cost_backend,
    run_preflight_smoke,
    validate_backend_name,
)
from app.resource_envelope import (
    resolve_project_hardware_policy,
    resolve_project_require_local_runner,
    resolve_project_spend_ceiling,
    resolve_project_spend_window_days,
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
from app.json_extract import scan_balanced_json_objects
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
from app.compute_experiment_contract import resolve_compute_spend_approval
from app.runtime_config_state import runtime_config as _global_runtime_config
from app.schemas import redact_command_log_text
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
# Extra wall-clock window added to the declared deadline for GPU (accelerator)
# jobs to cover the Deep Learning VM cold-start. The provisioning phase can
# consume the entire declared deadline before the command even begins on a
# short job; this additive floor is independent of and stacked on top of the
# declared runtime budget so provisioning-phase deaths are distinguishable from
# runtime-overrun kills.
GPU_PROVISIONING_ALLOWANCE_SECONDS = int(
    os.getenv("COMPUTE_GPU_PROVISIONING_ALLOWANCE_SECONDS", "900")
)
# Grace added on top of the job's full worst-case window (declared deadline +
# provisioning allowance) before the backend reaper ages a parked
# compute_experiment task out (park_wake_timeout). The monitor fires the
# compute_job_terminal wake well before this, so the deadline is only a
# backstop against a wedged monitor — generous so a healthy run never trips it.
_COMPUTE_PARK_GRACE_SECONDS = 600
# Short, independent budget for the provisioning phase (QUEUED/STARTING). A VM
# that never leaves the provisioning phase will be marked TIMED_OUT after this
# many seconds rather than after the full runtime budget. GPU jobs add the
# existing GPU_PROVISIONING_ALLOWANCE_SECONDS on top. Once a job reaches
# RUNNING the sub-deadline no longer applies; only the runtime deadline governs.
COMPUTE_PROVISIONING_DEADLINE_SECONDS = int(
    os.getenv("COMPUTE_PROVISIONING_DEADLINE_SECONDS", "360")
)
# After this many consecutive poll failures (status or cost) with no successful
# poll between them, the monitor terminates the job rather than leaving it RUNNING
# indefinitely. Default 20 ≈ 5 minutes at the 15-second poll interval.
COMPUTE_MAX_CONSECUTIVE_POLL_FAILURES = int(
    os.getenv("COMPUTE_MAX_CONSECUTIVE_POLL_FAILURES", "20")
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
    ComputeJobOutcome.SMOKE_FAILED: (False, None),
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


async def _assert_compute_spend_not_gated(
    session,
    task: Task,
    spend_cap_usd: float,
) -> None:
    """Terminal chokepoint: reject compute_experiment job creation when spend
    approval is required but has not been operator-cleared.

    Fires regardless of which spawn site created the Task, so a spawn site
    that forgets to call resolve_compute_spend_approval() cannot silently
    bypass the gate.
    """
    task_type = str((task.first_payload or {}).get("type") or "")
    if task_type != "compute_experiment":
        return
    project = await session.get(Project, task.project_id)
    # Use the global runtime_config which already includes deployment-state settings.
    runtime_config: dict = dict(_global_runtime_config)
    direction: dict | None = None
    if project is not None:
        direction_id = str((task.context or {}).get("direction_id") or "")
        if direction_id:
            direction = next(
                (
                    d
                    for d in (getattr(project, "directions", None) or [])
                    if d.get("id") == direction_id
                ),
                None,
            )
    requires_approval, threshold = resolve_compute_spend_approval(
        runtime_config=runtime_config,
        direction=direction,
        spend_cap_usd=spend_cap_usd,
    )
    if not requires_approval:
        return
    if task.spend_approval_cleared_at is not None:
        return
    raise ComputeLaunchPreconditionFailed({
        "error": "spend_cap_requires_approval",
        "message": (
            f"compute_experiment spend_cap_usd={spend_cap_usd:.2f} exceeds "
            f"project threshold of {threshold:.2f} USD — "
            "operator approval is required before this job can launch"
        ),
        "spend_cap_usd": spend_cap_usd,
        "threshold_usd": threshold,
    })


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
    setup_step: str | None = None,
    container_image: str | None = None,
    container_registry_auth: dict | None = None,
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

    if backend_name is not None:
        normalized_backend = backend_name
    elif accel_spec is not None:
        # GPU job: route to local GPU when present, fall back to cloud.
        normalized_backend = (
            LOCAL_GPU_BACKEND_NAME
            if (await asyncio.to_thread(detect_local_gpu)) is not None
            else GCP_BACKEND_NAME
        )
    else:
        normalized_backend = default_compute_backend_name()
    # Validate BEFORE persisting: an unknown / mode-incompatible backend would
    # otherwise wedge the row in QUEUED forever (monitor raises every pass).
    validate_backend_name(normalized_backend)
    # A GPU can never be provisioned by the CPU-only local-subprocess backend —
    # reject loudly at create time rather than silently running the command CPU-only.
    # runner-subprocess is allowed: GPU is on the operator's own hardware.
    if accel_spec is not None and normalized_backend not in (
        LOCAL_GPU_BACKEND_NAME,
        GCP_BACKEND_NAME,
        RUNNER_SUBPROCESS_BACKEND_NAME,
    ):
        raise ValueError(
            f"GPU compute jobs require the local-gpu, gcp, or runner-subprocess backend (got {normalized_backend!r})"
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
    # Runner-pinned tasks route to the runner-subprocess backend so execution
    # happens on operator hardware via the pull protocol. Override unconditionally
    # (the runner's GPU capability gate in _try_runner_dispatch already validated
    # that the runner can handle any GPU accelerator declared in the payload).
    runner_target_id: str | None = (locked_task.context or {}).get("runner_target_id") or None
    if runner_target_id is not None:
        normalized_backend = RUNNER_SUBPROCESS_BACKEND_NAME
    # Enforce-local policy: when a project requires its compute_experiment work to run
    # on a registered self-hosted local runner, a job that is NOT runner-pinned (no live
    # runner claimed the task → it would provision billable cloud compute) must be
    # rejected loudly. Pairs with the orchestrator's graceful dispatch hold (which keeps
    # such tasks QUEUED until a runner appears); this is the defense-in-depth guarantee
    # that no billable cloud GPU/VM is ever created for a require-local project.
    _policy_project = await session.get(Project, locked_task.project_id)
    _is_compute_experiment = str((locked_task.first_payload or {}).get("type") or "") == "compute_experiment"
    if (
        _policy_project is not None
        and _is_compute_experiment
        and resolve_project_require_local_runner(_policy_project)
        and normalized_backend != RUNNER_SUBPROCESS_BACKEND_NAME
    ):
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "local_runner_required",
                "message": (
                    "project policy require_local_runner is set: this compute_experiment "
                    "must run on a registered self-hosted local runner, but no live runner "
                    "claimed the task — refusing to provision billable cloud compute."
                ),
                "backend": normalized_backend,
            }
        )
    await _assert_compute_spend_not_gated(session, locked_task, capped_spend)
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
        container_image=container_image,
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
    await _check_project_hardware_policy(
        session,
        project_id=locked_task.project_id,
        machine_type=resolved_machine_type,
        accelerator=persisted_accelerator,
    )
    await _check_project_compute_job_concurrency_cap(
        session,
        project_id=locked_task.project_id,
    )
    await _check_project_spend_ceiling(
        session,
        project_id=locked_task.project_id,
        requested_cap_usd=capped_spend,
    )
    # GPU jobs add a provisioning allowance to the wall-clock deadline so a
    # Deep Learning VM cold-start cannot exhaust the declared runtime budget
    # before the command begins. The runtime budget itself is unchanged.
    provisioning_allowance = (
        GPU_PROVISIONING_ALLOWANCE_SECONDS if persisted_accelerator is not None else 0
    )
    deadline_at = _utcnow() + timedelta(
        seconds=capped_deadline + provisioning_allowance
    )
    # Short, independent provisioning sub-deadline: fail fast if the VM never
    # leaves the provisioning phase rather than waiting the full runtime budget.
    # GPU jobs add GPU_PROVISIONING_ALLOWANCE_SECONDS on top. The sub-deadline
    # is clamped to wall_clock_deadline so it never extends beyond the job's
    # overall budget.
    provisioning_budget_seconds = (
        COMPUTE_PROVISIONING_DEADLINE_SECONDS + provisioning_allowance
    )
    provisioning_deadline_at = min(
        _utcnow() + timedelta(seconds=provisioning_budget_seconds),
        deadline_at,
    )
    # Operator/local backends (local-subprocess, local-gpu) run on hardware Blueprint
    # does not pay cloud rates for — persist $0 so accrual is genuinely zero.
    # GCP retains the full accelerator-aware rate (base + GPU surcharge).
    if is_zero_cost_backend(normalized_backend):
        rate = 0.0
    else:
        rate = get_compute_rate_per_second(
            machine_type=resolved_machine_type,
            accelerator=persisted_accelerator,
            count=accelerator_count,
            spot=normalized_provisioning == "SPOT",
        )
    # Park deadline: worst-case window (declared runtime + provisioning allowance)
    # plus grace. Stored in polling_metadata so the agent can read it from the
    # job response instead of recomputing from the declared deadline alone.
    park_deadline_seconds = (
        capped_deadline + provisioning_allowance + _COMPUTE_PARK_GRACE_SECONDS
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
        "park_deadline_seconds": park_deadline_seconds,
    }
    if setup_step is not None:
        polling_metadata["setup_step"] = setup_step
    if container_image:
        polling_metadata["container_image"] = container_image
        if container_registry_auth and isinstance(container_registry_auth, dict):
            if container_registry_auth.get("password"):
                if normalized_backend != RUNNER_SUBPROCESS_BACKEND_NAME:
                    raise ValueError(
                        "container_registry_auth.password cannot be delivered to the "
                        "compute backend in the orchestrated path — the password is "
                        "never persisted, and there is no re-attachment channel at "
                        "launch time (unlike repo tokens, which are freshly minted). "
                        "Use a container image in a public registry or one accessible "
                        "via the VM's service-account identity (e.g. Artifact Registry "
                        "with Workload Identity). Omit the password field or use a "
                        "public image to proceed."
                    )
                # Runner-subprocess backend: persist full auth (server + username +
                # password) so the runner can docker login before pulling the image.
                # The password lives only in this row's JSONB and is scoped to the
                # runner-subprocess pull path — never sent to GCP or logged.
                registry_meta: dict[str, str] = {}
                if container_registry_auth.get("server"):
                    registry_meta["server"] = str(container_registry_auth["server"])
                if container_registry_auth.get("username"):
                    registry_meta["username"] = str(container_registry_auth["username"])
                registry_meta["password"] = str(container_registry_auth["password"])
                polling_metadata["container_registry_auth"] = registry_meta
            else:
                # No password: persist only server + username — password is never
                # written to the row for non-runner backends.
                registry_meta = {}
                if container_registry_auth.get("server"):
                    registry_meta["server"] = str(container_registry_auth["server"])
                if container_registry_auth.get("username"):
                    registry_meta["username"] = str(container_registry_auth["username"])
                if registry_meta:
                    polling_metadata["container_registry_auth"] = registry_meta
    if live_release_evidence is not None:
        polling_metadata[_LIVE_PROOF_METADATA_KEY] = live_release_evidence
    if runner_target_id is not None:
        polling_metadata["runner_target_id"] = runner_target_id
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
            "provisioning_deadline_at": provisioning_deadline_at.isoformat(),
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
        .order_by(
            ReleaseVersion.promoted_at.desc(), ReleaseVersion.version_number.desc()
        )
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
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
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

    Computes total rolling spend as: settled terminal-job costs + in-flight accrued
    compute costs + agent-VM/LLM task spend (via compute_project_window_spend_usd).
    If that total plus the requested cap would meet or exceed the configured
    ``resource_envelope.spend_ceiling_usd``, the launch is rejected before any row
    is written. The check is skipped when no ceiling is configured.
    """
    project = await session.get(Project, project_id)
    ceiling_usd = resolve_project_spend_ceiling(project) if project else None
    if not ceiling_usd:
        return
    window_days = resolve_project_spend_window_days(project)
    since = _utcnow() - timedelta(days=window_days)
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

    task_spend = await compute_project_window_spend_usd(
        session, project_id, window_hours=window_days * 24
    )
    rolling = compute_rolling + task_spend
    if rolling + requested_cap_usd >= ceiling_usd:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "project_spend_ceiling_exceeded",
                "message": (
                    f"project rolling {window_days}-day total spend ${rolling:.4f} (compute: "
                    f"${compute_rolling:.4f}, agent VM+LLM: ${task_spend:.4f}) plus "
                    f"requested cap ${requested_cap_usd:.4f} would meet or exceed configured "
                    f"ceiling ${ceiling_usd:.4f}"
                ),
                "rolling_spend_usd": rolling,
                "compute_spend_usd": compute_rolling,
                "task_spend_usd": task_spend,
                "requested_cap_usd": requested_cap_usd,
                "ceiling_usd": ceiling_usd,
            }
        )


async def _check_project_hardware_policy(
    session,
    *,
    project_id: uuid.UUID,
    machine_type: str | None,
    accelerator: str | None,
) -> None:
    """Raise ComputeLaunchPreconditionFailed when the job violates the project hardware policy.

    GPU jobs (accelerator is not None) are checked against allowed_accelerator_types;
    CPU jobs (accelerator is None) are checked against allowed_machine_types.
    The check is skipped when the relevant policy list is None (unlimited).
    """
    project = await session.get(Project, project_id)
    if project is None:
        return
    allowed_machines, allowed_accels = resolve_project_hardware_policy(project)
    if accelerator is not None:
        if allowed_accels is not None and accelerator not in allowed_accels:
            raise ComputeLaunchPreconditionFailed(
                {
                    "error": "project_hardware_policy_violated",
                    "message": (
                        f"accelerator {accelerator!r} is not in the project's "
                        f"allowed_accelerator_types {allowed_accels!r}"
                    ),
                    "requested": accelerator,
                    "allowed": allowed_accels,
                }
            )
    else:
        if allowed_machines is not None and machine_type not in allowed_machines:
            raise ComputeLaunchPreconditionFailed(
                {
                    "error": "project_hardware_policy_violated",
                    "message": (
                        f"machine type {machine_type!r} is not in the project's "
                        f"allowed_machine_types {allowed_machines!r}"
                    ),
                    "requested": machine_type,
                    "allowed": allowed_machines,
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
    container_image: str | None = None,
) -> str:
    payload = {
        "accelerator": accelerator,
        "accelerator_count": accelerator_count,
        "backend": backend_name,
        "command": command,
        "container_image": container_image,
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
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
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
    # Use the short provisioning sub-deadline stored at job creation. Fall back
    # to wall_clock_deadline for legacy rows that predate the sub-deadline field.
    prov_deadline = _parse_naive_iso_datetime(
        (job.polling_metadata or {}).get("provisioning_deadline_at")
    )
    deadline = prov_deadline if prov_deadline is not None else job.wall_clock_deadline
    if deadline is None:
        return None
    return max(0.0, (deadline - now).total_seconds())


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
    limit: int = 50,
    offset: int = 0,
    all_terminal: bool = False,
) -> tuple[list[ComputeJob], bool]:
    """Return a project's active compute jobs plus recently-finished ones.

    Active jobs are always included. When ``all_terminal=True``, ALL terminal
    jobs are included regardless of age (``include_finished_within`` is ignored
    for the terminal filter). Otherwise only terminal jobs completed within
    ``include_finished_within`` are returned. Ordered newest-first.
    """
    if all_terminal:
        terminal_filter = ComputeJob.completed_at.is_not(None)
    else:
        cutoff = _utcnow() - include_finished_within
        terminal_filter = ComputeJob.completed_at.is_not(None) & (
            ComputeJob.completed_at >= cutoff
        )
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.project_id == project_id,
            (
                ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES)
                | terminal_filter
            ),
        )
        .order_by(ComputeJob.created_at.desc(), ComputeJob.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    jobs = list(result.scalars().all())
    has_more = len(jobs) > limit
    return jobs[:limit], has_more


# Aggregate helpers live in compute_jobs_aggregation.py (extracted to keep this
# module under the line-count cap). Re-export for backwards-compatibility.
from app.compute_jobs_aggregation import (  # noqa: E402,F401
    aggregate_all_compute_jobs_summary,
    aggregate_compute_jobs_summary,
    aggregate_criterion_summary,
)


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


def _smoke_fail_reason(smoke: PreflightSmokeResult, declared_metric: str | None) -> str:
    if not smoke.startup_ok:
        excerpt = (smoke.error_excerpt or "")[:300]
        return (
            f"compute_preflight_smoke_failed: command exited {smoke.exit_code} on "
            f"DB-less substrate before full VM provisioned; stderr: {excerpt}"
        )[:500]
    # startup_ok but metric missing
    return (
        f"compute_preflight_smoke_failed: command ran but declared metric "
        f"{declared_metric!r} not found in result file (no full VM provisioned)"
    )[:500]


async def _run_and_apply_preflight_smoke(
    session_factory,
    job: ComputeJob,
    task: Task | None,
) -> None:
    """Run a pre-flight smoke on the job's command and persist the result.

    On PASS: stamps ``preflight_smoke_done=True`` so the next monitor tick
    proceeds to the normal provisioning path.
    On FAIL: finalizes the job as FAILED/SMOKE_FAILED and fires the task wake.
    """
    meta = job.polling_metadata or {}
    command = str(meta.get("command") or "")
    env = {str(k): str(v) for k, v in (meta.get("env") or {}).items()}
    cwd = meta.get("cwd") or None
    setup_step = meta.get("setup_step") or None
    # Containerize the smoke when the experiment spec declared an image, mirroring
    # the launch path's reconstruction (see _build_launch_spec / create_compute_job).
    # The persisted auth carries ONLY server/username — the password is stripped at
    # create time (see create_compute_job), so docker login is skipped and
    # gated-registry pulls may fail at smoke; public images are still validated.
    container_image = meta.get("container_image") or None
    registry_auth_meta = meta.get("container_registry_auth")
    container_registry_auth = (
        dict(registry_auth_meta) if isinstance(registry_auth_meta, dict) else None
    )

    payload = task.first_payload if task is not None else {}
    criterion = (payload or {}).get("success_criterion") or {}
    declared_metric: str | None = (
        (payload or {}).get("declared_metric")
        or (criterion.get("metric") if isinstance(criterion, dict) else None)
        or None
    )
    # Arms-synthesized contracts write {"arms": {name: {metric: value}}} to the
    # result file, not a flat top-level key.  The original task spec requires
    # arms jobs to check startup_ok only; skip the metric assertion for them.
    is_arms = bool((payload or {}).get("arms"))
    declared_metric_for_smoke = None if is_arms else declared_metric

    smoke = await run_preflight_smoke(
        command,
        declared_metric=declared_metric_for_smoke,
        env=env,
        cwd=cwd,
        setup_step=setup_step,
        container_image=container_image,
        container_registry_auth=container_registry_auth,
    )

    now = _utcnow()
    # A timeout (exit_code is None) means the command is still alive = startup OK.
    # Only enforce the metric check when the command EXITED CLEANLY (exit_code == 0).
    # Rejecting on timeout would false-reject GPU training commands that declare a
    # metric but cannot finish within the short smoke window.
    smoke_passed = smoke.startup_ok and (
        smoke.exit_code is None or declared_metric_for_smoke is None or smoke.metric_captured
    )
    # When a container_image was declared but Docker was unavailable on the
    # orchestrator host, the smoke ran bare bash — not a faithful test of the
    # containerized command.  A bare-run failure MUST NOT gate the job: the
    # command may legitimately depend on in-container libs not present on the
    # host.  Treat the degraded smoke as a non-binding pass so the job
    # proceeds to the real VM where the container will be available.
    if smoke.degraded_no_docker and not smoke_passed:
        smoke_passed = True

    async with session_factory() as session:
        live = await _lock_compute_job(session, job.id)
        if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            return
        if smoke_passed:
            _set_meta(
                live,
                preflight_smoke_done=True,
                preflight_smoke_status="passed",
                preflight_smoke_containerized=smoke.ran_containerized,
                preflight_smoke_degraded_no_docker=smoke.degraded_no_docker,
            )
            live.updated_at = now
            session.add(live)
            await session.commit()
            return
        # Smoke failed: finalize immediately — no full VM will be provisioned.
        reason = _smoke_fail_reason(smoke, declared_metric)
        diagnostic: dict[str, Any] = {
            "startup_ok": smoke.startup_ok,
            "exit_code": smoke.exit_code,
            "metric_captured": smoke.metric_captured,
            "error_excerpt": (smoke.error_excerpt or "")[:500],
            "containerized": smoke.ran_containerized,
            "degraded_no_docker": smoke.degraded_no_docker,
        }
        if declared_metric:
            diagnostic["declared_metric"] = declared_metric
        _set_meta(
            live,
            preflight_smoke_done=True,
            preflight_smoke_status="failed",
            preflight_smoke_diagnostic=diagnostic,
        )
        _finalize_terminal_job(
            live,
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.SMOKE_FAILED,
            terminal_reason=reason,
            now=now,
        )
        await _link_terminal_artifact(session, live)
        if live.task_id is not None:
            from app.task_lifecycle import fire_task_wakes  # noqa: PLC0415

            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(live.id),
            )
        await session.commit()
    logger.info(
        "[COMPUTE_JOB] pre-flight smoke FAILED for job=%s: %s",
        job.id,
        reason,
    )


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
        # Pre-flight smoke: run ONCE on QUEUED jobs before reserving the full launch.
        # Idempotent: the flag in polling_metadata guards against re-running on restart.
        # Skip for runner-subprocess: the command must run on operator hardware
        # (may need local GPU/CUDA/data) — a leader-local smoke is wrong and would
        # always validate a different substrate than the real execution environment.
        if job.status == ComputeJobStatus.QUEUED and not (
            job.polling_metadata or {}
        ).get("preflight_smoke_done") and job.backend != RUNNER_SUBPROCESS_BACKEND_NAME:
            await _run_and_apply_preflight_smoke(session_factory, job, task)
            return  # next tick picks up STARTING (pass) or FAILED/SMOKE_FAILED (fail)

        reserved = await _reserve_compute_job_launch(
            session_factory, job.id, backend, now
        )
        if reserved is None:
            return
        provisioning_timeout_seconds = _provisioning_timeout_seconds(job, now=now)
        if provisioning_timeout_seconds == 0.0:
            # Guard: if the VM has heartbeated (first heartbeat already transitions
            # the job to RUNNING via the endpoint, but check reserved which is a
            # freshly-locked row in case the endpoint and the monitor race).
            if reserved.last_heartbeat_at is not None:
                logger.info(
                    "[COMPUTE_JOB] provisioning deadline passed but VM %s heartbeated"
                    " — skipping termination; next poll will handle via running path",
                    reserved.external_compute_id,
                )
                return
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
                next_poll_at=now
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
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
            spec = await _attach_repo_clone(
                session_factory, reserved, _build_launch_spec(reserved)
            )
            launch_coro = backend.launch(spec)
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
            # Guard: re-read the job to check if the VM heartbeated during the
            # wait.  A heartbeat means the VM booted successfully despite the
            # slow GCP LRO; the endpoint already transitioned it to RUNNING.
            # Skipping termination here lets the next monitor tick handle it
            # via the normal running/polling path.
            async with session_factory() as _hb_session:
                _fresh = await _hb_session.get(ComputeJob, reserved.id)
            if _fresh is not None and _fresh.last_heartbeat_at is not None:
                logger.info(
                    "[COMPUTE_JOB] launch LRO timed out but VM %s heartbeated"
                    " — skipping termination; next poll will handle via running path",
                    reserved.external_compute_id,
                )
                # The heartbeat endpoint transitions STARTING→RUNNING atomically.
                # Under a tight race the monitor may still see STARTING here;
                # finish the transition so the job doesn't stay stuck.
                if _fresh.status == ComputeJobStatus.STARTING:
                    _now = _utcnow()
                    async with session_factory() as _tr_session:
                        _tr_job = await _lock_compute_job(_tr_session, reserved.id)
                        if (
                            _tr_job is not None
                            and _tr_job.status == ComputeJobStatus.STARTING
                        ):
                            _tr_job.status = ComputeJobStatus.RUNNING
                            _tr_job.started_at = _tr_job.started_at or _now
                            _tr_job.updated_at = _now
                            await _tr_session.commit()
                return
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
                next_poll_at=now
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
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
        # For runner-subprocess, the STARTING→RUNNING transition is performed by
        # the runner when it claims the job via /api/runner/next-compute-job.
        # Calling _apply_launch_result here would move the job to RUNNING before
        # the runner has a chance to claim it, making /next-compute-job miss it.
        if job.backend != RUNNER_SUBPROCESS_BACKEND_NAME:
            await _apply_launch_result(
                session_factory,
                job.id,
                result,
                now=now,
            )
        elif result.metadata:
            # Persist launch metadata (e.g. repo_clone) without transitioning
            # to RUNNING — the runner does that when it claims the job.
            async with session_factory() as session:
                live = await _lock_compute_job(session, job.id)
                if live is not None and live.status in _COMPUTE_JOB_PRELAUNCH_STATUSES:
                    _set_meta(live, **result.metadata)
                    await session.commit()
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
        _next_retry_at = failed_at + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS)
        # When the wall-clock deadline has already passed, terminate immediately
        # rather than deferring to the next poll cycle — a persistently
        # unreachable backend would otherwise keep the job RUNNING forever.
        if job.wall_clock_deadline is not None and now >= job.wall_clock_deadline:
            terminate_diagnostic = _build_compute_rpc_diagnostic(
                job,
                operation="poll_status",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=None,
                reason="poll_unreachable_at_deadline",
            )
            terminated = True
            if job.external_compute_id:
                try:
                    terminated = await backend.terminate(job, reason="deadline")
                except Exception:
                    logger.exception(
                        "[COMPUTE_JOB] terminate failed for unreachable job=%s at deadline",
                        job.id,
                    )
                    terminated = False
            await _apply_polled_compute_job_result(
                session_factory,
                job.id,
                now=now,
                poll_attempt_count=(job.poll_attempt_count or 0) + 1,
                next_poll_at=now
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
                status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
                accrued_cost_usd=float(job.accrued_cost_usd or 0.0),
                terminal_status=ComputeJobStatus.TIMED_OUT,
                outcome=ComputeJobOutcome.TIMED_OUT,
                terminal_reason=(
                    "compute job exceeded deadline; backend unreachable at kill time"
                ),
                terminate_ok=terminated,
                compute_rpc_diagnostic=terminate_diagnostic,
            )
            return
        _updated = await _record_compute_rpc_failure(
            session_factory,
            job.id,
            operation="poll_status",
            exc=exc,
            started_at=started_at,
            failed_at=failed_at,
            next_retry_at=_next_retry_at,
        )
        if await _check_poll_failure_safety_kills(
            _updated,
            backend,
            session_factory,
            now=now,
            poll_attempt_count=(job.poll_attempt_count or 0) + 1,
            next_poll_at=_next_retry_at,
        ):
            return
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
            _updated = await _record_compute_rpc_failure(
                session_factory,
                job.id,
                operation="poll_cost",
                exc=exc,
                started_at=started_at,
                failed_at=failed_at,
                next_retry_at=failed_at
                + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
            )
            if await _check_poll_failure_safety_kills(
                _updated,
                backend,
                session_factory,
                now=now,
                poll_attempt_count=poll_attempt_count,
                next_poll_at=next_poll_at,
            ):
                return
            return
        accrued_cost_usd = max(float(job.accrued_cost_usd or 0.0), cost.total_cost_usd)
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
        _updated = await _record_compute_rpc_failure(
            session_factory,
            job.id,
            operation="poll_cost",
            exc=exc,
            started_at=started_at,
            failed_at=failed_at,
            next_retry_at=failed_at
            + timedelta(seconds=COMPUTE_JOB_MONITOR_INTERVAL_SECONDS),
        )
        if await _check_poll_failure_safety_kills(
            _updated,
            backend,
            session_factory,
            now=now,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
        ):
            return
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
        salvaged = await _salvage_poll_status(backend, job)
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=now,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=_merge_salvaged_status(status, salvaged),
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
        salvaged = await _salvage_poll_status(backend, job)
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=now,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=_merge_salvaged_status(status, salvaged),
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


_SALVAGE_POLL_TIMEOUT_SECONDS = float(
    os.getenv("COMPUTE_SALVAGE_POLL_TIMEOUT_SECONDS", "10")
)


async def _salvage_poll_status(
    backend, job: ComputeJob, *, timeout_seconds: float = _SALVAGE_POLL_TIMEOUT_SECONDS
) -> "ComputeStatusResult | None":
    """Best-effort post-terminate poll — never raises, never hangs the monitor."""
    try:
        return await asyncio.wait_for(backend.poll_status(job), timeout=timeout_seconds)
    except Exception as exc:
        logger.warning(
            "[COMPUTE_JOB] salvage poll failed for job=%s (ignored): %s",
            job.id,
            exc,
        )
        return None


def _merge_salvaged_status(
    base: "ComputeStatusResult", salvaged: "ComputeStatusResult | None"
) -> "ComputeStatusResult":
    """Return a new status snapshot with late-arriving fields from salvaged merged in.

    The terminal outcome, status code, and cost semantics all come from ``base``.
    Only metrics, command_log, and echoed_release_sha are overridden by non-None
    values from the post-terminate poll so late-emitted evidence is not dropped.
    """
    if salvaged is None:
        return base
    return ComputeStatusResult(
        status=base.status,
        message=base.message,
        exit_code=base.exit_code,
        finished_at=base.finished_at,
        metrics=salvaged.metrics if salvaged.metrics is not None else base.metrics,
        command_log=salvaged.command_log
        if salvaged.command_log is not None
        else base.command_log,
        echoed_release_sha=salvaged.echoed_release_sha
        if salvaged.echoed_release_sha is not None
        else base.echoed_release_sha,
    )


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
        if status.echoed_release_sha is not None:
            _set_meta(live, release_echo_sha=status.echoed_release_sha)
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
            if status.live_output is not None:
                _set_meta(live, live_output=dict(status.live_output))
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
    env = {str(k): str(v) for k, v in (meta.get("env") or {}).items()}
    live_evidence = meta.get(_LIVE_PROOF_METADATA_KEY)
    if isinstance(live_evidence, dict):
        release_env = {
            "BLUEPRINT_RELEASE_SHA": str(live_evidence.get("promoted_sha") or ""),
            "BLUEPRINT_RELEASE_VERSION": str(
                live_evidence.get("release_version_number") or ""
            ),
            "BLUEPRINT_RELEASE_VERSION_ID": str(
                live_evidence.get("release_version_id") or ""
            ),
        }
        # Caller-provided env takes precedence over release defaults.
        env = {**release_env, **env}
    # Use the short provisioning sub-deadline; fall back to wall_clock_deadline
    # for legacy rows that predate the sub-deadline field.
    prov_deadline = _parse_naive_iso_datetime(meta.get("provisioning_deadline_at"))
    effective_provisioning_deadline = (
        prov_deadline if prov_deadline is not None else job.wall_clock_deadline
    )
    registry_auth_meta = meta.get("container_registry_auth")
    container_registry_auth = (
        dict(registry_auth_meta) if isinstance(registry_auth_meta, dict) else None
    )
    return ComputeLaunchSpec(
        job_id=job.id,
        external_id=job.external_compute_id,
        command=str(meta.get("command") or ""),
        machine_type=job.machine_type or "",
        spend_cap_usd=float(job.spend_cap_usd or 0.0),
        deadline_at=job.wall_clock_deadline,
        provisioning_deadline_at=effective_provisioning_deadline,
        runtime_deadline_seconds=int(meta.get("requested_deadline_seconds") or 0)
        or None,
        cwd=meta.get("cwd"),
        env=env,
        provisioning_model=str(meta.get("provisioning_model") or "STANDARD"),
        accelerator=job.accelerator_type,
        accelerator_count=int(job.accelerator_count or 1),
        setup_step=meta.get("setup_step") or None,
        container_image=meta.get("container_image") or None,
        container_registry_auth=container_registry_auth,
    )


async def _attach_repo_clone(
    session_factory, job: ComputeJob, spec: ComputeLaunchSpec
) -> ComputeLaunchSpec:
    """Best-effort: give the compute VM a checked-out clone of the project repo.

    Compute commands routinely import the project's package (``from pkg ...`` /
    ``python3 -m pkg``); on a bare VM with no repo they die with ModuleNotFound
    before reporting any metric, which surfaces as a silent "no terminal marker"
    FAILURE. We resolve the project's repo + a FRESH installation token at launch
    (never persisted) and stamp them onto the spec so the startup script clones
    the repo, cds into it, and puts it on PYTHONPATH before the command runs.

    The ref is the branch the experiment targets: the task's ``base_branch`` (set
    by research/plan/operator authoring) → ``context.original_base`` → the
    project's promotion target → ``main``.

    Defensive by contract: a project with no repo, a token-mint failure, or any
    other error degrades to NO checkout (legacy behavior) — it NEVER blocks the
    launch. A genuinely repo-dependent command then fails the same way it does
    today, no worse.
    """
    if not job.project_id:
        return spec
    try:
        async with session_factory() as session:
            project = await session.get(Project, job.project_id)
            if project is None or not project.github_owner or not project.github_repo:
                return spec
            ref: str | None = None
            if job.task_id:
                task = await session.get(Task, job.task_id)
                if task is not None:
                    payload = (
                        task.first_payload
                        if isinstance(task.first_payload, dict)
                        else {}
                    )
                    ctx = task.context if isinstance(task.context, dict) else {}
                    ref = payload.get("base_branch") or ctx.get("original_base")
            ref = ref or getattr(project, "promotion_target", None) or "main"
            # Reuses the shared, cached install-token path (50-min TTL) so launches
            # don't drain the App-JWT bucket.
            from app.branch_cleanup import _get_project_github_token

            token = await _get_project_github_token(project, session)
            if not token:
                logger.warning(
                    "[COMPUTE_JOB] no GitHub token for project=%s; launching job=%s "
                    "without a repo checkout",
                    job.project_id,
                    job.id,
                )
                return spec
            # staging branches are deleted after the plan rolls up to integration.
            # If the resolved ref is a staging branch that no longer exists, degrade
            # to the project's integration/promotion branch where the code has landed.
            if project.is_staging_branch(ref):
                from app.github_refs import get_branch_sha

                try:
                    await get_branch_sha(
                        token,
                        str(project.github_owner),
                        str(project.github_repo),
                        ref,
                    )
                except Exception:
                    fallback = getattr(project, "promotion_target", None) or "main"
                    logger.info(
                        "[COMPUTE_JOB] plan branch %r not found for job=%s; "
                        "falling back to %r",
                        ref,
                        job.id,
                        fallback,
                    )
                    ref = fallback
            spec.repo_clone = {
                "owner": str(project.github_owner),
                "repo": str(project.github_repo),
                "ref": str(ref),
            }
            spec.repo_token = token
            return spec
    except Exception:  # noqa: BLE001 — repo checkout is best-effort, never fatal
        logger.warning(
            "[COMPUTE_JOB] repo-clone attach failed for job=%s; launching without a "
            "repo checkout",
            job.id,
            exc_info=True,
        )
        return spec


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


def _bounded_rpc_string(
    value: Any, *, limit: int = _COMPUTE_RPC_DIAGNOSTIC_MAX_STRING
) -> str:
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


async def _check_poll_failure_safety_kills(
    updated_job: "ComputeJob | None",
    backend,
    session_factory,
    *,
    now: datetime,
    poll_attempt_count: int,
    next_poll_at: datetime,
) -> bool:
    """After a poll failure, check whether a safety kill should be applied.

    Checks consecutive-failure threshold, wall-clock deadline, and estimated
    spend cap — in that order. If any condition is met, terminates the backend
    best-effort and records a terminal transition. Returns True if a kill fired.
    """
    if updated_job is None:
        return False

    diag = (updated_job.polling_metadata or {}).get(_COMPUTE_RPC_DIAGNOSTIC_KEY)
    attempt_count = int(diag.get("attempt_count", 0) if isinstance(diag, dict) else 0)

    terminal_status: ComputeJobStatus | None = None
    outcome: ComputeJobOutcome | None = None
    terminal_reason: str | None = None

    if attempt_count >= COMPUTE_MAX_CONSECUTIVE_POLL_FAILURES:
        terminal_status = ComputeJobStatus.FAILED
        outcome = ComputeJobOutcome.FAILURE
        terminal_reason = (
            f"compute job terminated: backend unreachable after"
            f" {attempt_count} consecutive poll failures"
        )
    else:
        runtime_deadline = _runtime_deadline_at(updated_job)
        wall_clock_deadline = updated_job.wall_clock_deadline
        if (runtime_deadline is not None and runtime_deadline <= now) or (
            wall_clock_deadline is not None and wall_clock_deadline <= now
        ):
            terminal_status = ComputeJobStatus.TIMED_OUT
            outcome = ComputeJobOutcome.TIMED_OUT
            terminal_reason = "compute job exceeded runtime deadline"
        elif updated_job.spend_cap_usd is not None:
            accrued = float(updated_job.accrued_cost_usd or 0.0)
            cost_rate = float(
                (updated_job.polling_metadata or {}).get("cost_rate_per_second") or 0.0
            )
            if updated_job.started_at is not None and cost_rate > 0:
                elapsed = max(0.0, (now - updated_job.started_at).total_seconds())
                estimated_cost = max(accrued, cost_rate * elapsed)
            else:
                estimated_cost = accrued
            if estimated_cost >= float(updated_job.spend_cap_usd):
                terminal_status = ComputeJobStatus.FAILED
                outcome = ComputeJobOutcome.SPEND_CAP_EXCEEDED
                terminal_reason = "compute job terminated at spend cap"

    if terminal_status is None:
        return False

    try:
        await backend.terminate(updated_job, reason=terminal_reason)
    except Exception:
        logger.exception(
            "[COMPUTE_JOB] poll-failure safety-kill terminate failed for job=%s",
            updated_job.id,
        )

    await _apply_polled_compute_job_result(
        session_factory,
        updated_job.id,
        now=now,
        poll_attempt_count=poll_attempt_count,
        next_poll_at=next_poll_at,
        status=ComputeStatusResult(status=ComputeJobStatus.FAILED),
        accrued_cost_usd=float(updated_job.accrued_cost_usd or 0.0),
        terminal_status=terminal_status,
        outcome=outcome,
        terminal_reason=terminal_reason,
    )
    return True


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
    # Erase the registry password the moment the job reaches a terminal state.
    # The runner only needed it once (at docker-login time); keeping it in the
    # DB row beyond that is an unnecessary plaintext secret at rest.
    meta = dict(job.polling_metadata or {})
    if isinstance(meta.get("container_registry_auth"), dict):
        auth = dict(meta["container_registry_auth"])
        auth.pop("password", None)
        meta["container_registry_auth"] = auth
        job.polling_metadata = meta


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
        return (
            value
            if value == value and value not in (float("inf"), float("-inf"))
            else None
        )
    if isinstance(value, str) and value.strip():
        try:
            num = float(value)
        except ValueError:
            return None
        return num if num == num and num not in (float("inf"), float("-inf")) else None
    return None


# Upper bound on how much captured command-log text the stdout->metrics fallback
# scans for a trailing JSON object. The balanced-JSON scan is super-linear in the
# number of "{" candidates, so a noisy multi-megabyte log can never make the
# fallback expensive: only the trailing window is scanned (the experiment prints
# its result object last, so last-object-wins lives in the tail).
_STDOUT_METRICS_SCAN_MAX_CHARS = 100_000


def _stdout_metrics_fallback(meta: dict) -> dict | None:
    """Recover experiment metrics from captured stdout when no result file exists.

    A command that writes ``{metric: value}`` JSON to ``$BLUEPRINT_RESULT_FILE``
    populates ``result_metrics`` directly; that path always wins. This fallback
    only fires when ``result_metrics`` is absent/empty: it scans the captured
    command log for the LAST balanced JSON object the command printed to
    stdout/stderr (last-object-wins) and returns its numeric keys so a metric
    that only reached stdout is still measurable.

    Bounded and defensive: scans at most the trailing
    ``_STDOUT_METRICS_SCAN_MAX_CHARS`` of the log, keeps only numeric keys, and
    returns ``None`` when there is no command log or no balanced JSON object. The
    caller filters reserved/backend-derived keys, so this never overwrites
    ``durable_file_count`` or a job-level field.
    """
    command_log = meta.get("command_log")
    if not isinstance(command_log, dict):
        return None
    text = command_log.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > _STDOUT_METRICS_SCAN_MAX_CHARS:
        text = text[-_STDOUT_METRICS_SCAN_MAX_CHARS:]
    obj = scan_balanced_json_objects(text, last=True)
    if not isinstance(obj, dict):
        return None
    numeric = {k: v for k, v in obj.items() if _coerce_metric_number(v) is not None}
    return numeric or None


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
    # Inject backend-derived durable-artifact metrics as top-level numeric keys.
    # These are stamped by _link_terminal_artifact and remain evaluable even when
    # the job timed out or crashed before writing a command result.  Command-reported
    # values (below) are NOT allowed to overwrite these reserved backend keys.
    _BACKEND_DERIVED_KEYS = ("durable_file_count", "durable_payload_bytes")
    for key in _BACKEND_DERIVED_KEYS:
        raw = meta.get(key)
        num = _coerce_metric_number(raw)
        if num is not None:
            result[key] = num
    # Flatten command-reported result metrics to top-level numeric keys so the
    # success-criterion gate can read them by name, exactly as the agent did. A
    # reported metric whose name collides with a job-level key is skipped so it
    # can never overwrite the reserved field.
    reported = meta.get("result_metrics")
    if not (isinstance(reported, dict) and reported):
        # No explicit result-file metrics: fall back to the LAST balanced JSON
        # object the command printed to stdout/stderr. An explicit
        # $BLUEPRINT_RESULT_FILE write (above) always wins; this only fills the
        # gap so a metric that only reached stdout is still measurable.
        reported = _stdout_metrics_fallback(meta)
    if isinstance(reported, dict):
        for name, value in reported.items():
            if name in result or name in _BACKEND_DERIVED_KEYS:
                continue
            num = _coerce_metric_number(value)
            if num is not None:
                result[name] = num
        # Preserve the arms block intact so the success-criterion evaluator can
        # read per-arm metrics using best-arm semantics. The numeric loop above
        # drops non-numeric values; the arms block (a nested dict) must survive
        # as-is for multi-arm experiments.
        if isinstance(reported.get("arms"), dict) and "arms" not in result:
            result["arms"] = reported["arms"]
    # Inject per-arm cost_usd by apportioning total cost by elapsed_seconds fraction.
    # Sum elapsed_seconds across all arms; if total is 0 or missing, skip injection.
    if final_cost is not None and isinstance(result.get("arms"), dict):
        arms_block = result["arms"]
        total_elapsed = 0.0
        for arm_dict in arms_block.values():
            if isinstance(arm_dict, dict):
                elapsed = arm_dict.get("elapsed_seconds")
                if isinstance(elapsed, (int, float)):
                    total_elapsed += float(elapsed)
        if total_elapsed > 0:
            for arm_dict in arms_block.values():
                if isinstance(arm_dict, dict):
                    elapsed = arm_dict.get("elapsed_seconds")
                    if isinstance(elapsed, (int, float)):
                        arm_dict["cost_usd"] = float(final_cost) * (float(elapsed) / total_elapsed)
    # Embed the bounded command log text so downstream analysis (knowledge doc,
    # LLM analysis/verdict prompt) can find results that live in stdout/stderr
    # rather than in $BLUEPRINT_RESULT_FILE. Capped at _COMMAND_LOG_EMBED_CHARS so
    # a noisy command log can never bloat the artifact JSON unreasonably. When the
    # log exceeds the cap, keep the HEAD (run setup) AND the TAIL (results) and
    # elide the middle — a head-only slice dropped exactly the end-of-run pass/fail
    # summary a verdict agent must reason over.
    _COMMAND_LOG_EMBED_CHARS = 16000
    _COMMAND_LOG_EMBED_HEAD_CHARS = 4000
    command_log = meta.get("command_log")
    if isinstance(command_log, dict):
        cl_text = str(command_log.get("text") or "").strip()
        if cl_text:
            redacted = redact_command_log_text(cl_text)
            if len(redacted) <= _COMMAND_LOG_EMBED_CHARS:
                embed = redacted
                embed_truncated = False
            else:
                head = redacted[:_COMMAND_LOG_EMBED_HEAD_CHARS]
                tail = redacted[
                    -(_COMMAND_LOG_EMBED_CHARS - _COMMAND_LOG_EMBED_HEAD_CHARS):
                ]
                elided = len(redacted) - len(head) - len(tail)
                embed = (
                    f"{head}\n...[{elided} chars elided — middle of log]...\n{tail}"
                )
                embed_truncated = True
            result["command_log_text"] = embed
            # Truncated if the runner already truncated OR we elided here.
            result["command_log_truncated"] = (
                bool(command_log.get("truncated")) or embed_truncated
            )
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
    # Stamp backend-derived durable metrics so build_compute_result_logs can
    # surface them as evaluable top-level keys even for timed-out/failed jobs.
    _set_meta(
        job,
        durable_file_count=reference.file_count,
        durable_payload_bytes=reference.total_size_bytes,
    )
    task = await session.get(Task, job.task_id)
    try:
        async with session.begin_nested():
            artifact_logs = {
                "compute_job_id": str(job.id),
                "backend": reference.backend,
                "artifact_uri": reference.uri,
                "file_count": reference.file_count,
                "outcome": (job.outcome.value if job.outcome is not None else None),
                "final_cost_usd": job.final_cost_usd,
            }
            command_log = (job.polling_metadata or {}).get("command_log")
            if command_log is not None:
                if isinstance(command_log, dict) and isinstance(
                    command_log.get("text"), str
                ):
                    command_log = dict(command_log)
                    command_log["text"] = redact_command_log_text(command_log["text"])
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
