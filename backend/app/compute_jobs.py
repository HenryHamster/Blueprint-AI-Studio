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
import math
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, and_
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.field_encryption import encrypt_field
from app.compute_backends import (
    GCP_BACKEND_NAME,
    LOCAL_GPU_BACKEND_NAME,
    RUNNER_SUBPROCESS_BACKEND_NAME,
    ComputeLaunchSpec,
    ComputeLaunchSpecError,
    ComputeStatusResult,
    PreflightSmokeResult,
    _parse_digest_from_pinned_ref,
    collect_terminal_artifact_reference,
    default_compute_backend_name,
    detect_local_gpu,
    get_compute_backend,
    is_zero_cost_backend,
    persist_viewable_artifacts_with_completeness,
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
from app.alert_dispatch import _maybe_create_no_progress_watchdog_alert
from app.database import AsyncSessionLocal
from app.compute_result_metrics import (
    coerce_metric_number as _coerce_metric_number,
    stdout_metrics_fallback as _stdout_metrics_fallback,
)
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobFile,
    ComputeJobOutcome,
    ComputeJobStatus,
    LocalRunner,
    Project,
    ReleaseVersion,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.compute_experiment_contract import resolve_compute_spend_approval
from app.runtime_config_state import runtime_config as _global_runtime_config
from app.schemas import redact_command_log_text
from app import storage
from app.time_utils import _utcnow, exponential_backoff
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
# Max retries for provisioning-timeout failures. When a compute job TIMED_OUT
# during the provisioning phase (before the VM reached RUNNING), the monitor
# retries up to this many times with backoff before going terminal. Transient
# GCP infrastructure issues (zone quota exhaustion, image family blips) are
# recovered without operator intervention.
MAX_PROVISIONING_RETRY_COUNT = int(
    os.getenv("COMPUTE_MAX_PROVISIONING_RETRY_COUNT", "2")
)
# Base backoff in seconds for each provisioning retry. The actual wait between
# retries is ``exponential_backoff(retry_number, base=this, cap=600)``, yielding
# roughly 2 min for retry 1 and 4 min for retry 2.
PROVISIONING_RETRY_BACKOFF_BASE_SECONDS = int(
    os.getenv("COMPUTE_PROVISIONING_RETRY_BACKOFF_SECONDS", "120")
)
# No-progress watchdog. A long-running compute job (e.g. multi-day RL training)
# can burn its entire wall-clock deadline while learning nothing. Progress is
# retained as a bounded receipt-time envelope in polling_metadata, written from
# authenticated runner heartbeats via append_progress_sample. These constants
# drive the consumer that reads that trend and cuts a stalled job early.
#
# The watchdog is FAIL-OPEN by construction: it fires ONLY when the job declares
# a metric to watch (polling_metadata['declared_metric']) AND enough progress
# samples exist to prove a sustained plateau. Jobs that report no progress
# telemetry, or declare no metric, are byte-for-byte unaffected. Setting the
# window to 0 disables the watchdog entirely.
COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS = float(
    os.getenv("COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS", "1800")
)
COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS = float(
    os.getenv("COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS", "1800")
)
COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES = int(
    os.getenv("COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES", "4")
)
# Relative movement below this fraction of the metric's magnitude over the whole
# window counts as "no progress" (direction-agnostic: a flat metric is a stall
# whether higher or lower is better).
COMPUTE_JOB_NO_PROGRESS_EPSILON = float(
    os.getenv("COMPUTE_JOB_NO_PROGRESS_EPSILON", "1e-4")
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

# Progress-metric history — bounded, server-timestamped time-series written into
# polling_metadata.  Samples are coalesced to one per metric/minute, which keeps
# a full 24-hour contract window (1441 points) even when a runner emits very
# high-frequency or unrelated-metric telemetry.
_PROGRESS_METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_PROGRESS_SAMPLE_BUCKET_SECONDS = 60
_PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC = 1500
_PROGRESS_SERIES_MAX_METRICS = 16

# Stable terminal-kind label for a no-progress early-cut.  Callers that cut a
# job for a stalled progress trend pass this as terminal_kind to
# _finalize_terminal_job so the disposition is distinct from a wall-clock expiry.
NO_PROGRESS_TERMINAL_REASON = "no_progress"

# Runner-subprocess silent-death timeout. A runner is expected to heartbeat
# every ~60s; after this many seconds without a heartbeat the job is reaped
# as a silent orphan, freeing the project concurrency slot and spend ceiling.
RUNNER_SILENT_TIMEOUT_SECONDS = int(
    os.getenv("COMPUTE_JOB_RUNNER_SILENT_TIMEOUT_SECONDS", "600")
)


def append_progress_sample(
    meta: dict, *, metric: str, value: float, t: str, received_at: datetime | str | None = None
) -> dict:
    """Append a progress sample to the ring buffer in a polling_metadata dict.

    Returns a new dict with the sample appended.  History is bounded per metric
    rather than globally, so chatty auxiliary metrics cannot evict the declared
    metric needed to enforce a long watchdog window.
    Silently ignores non-finite values, bad metric keys, or any other error
    so the caller never raises on malformed input.

    ``received_at`` is the authenticated server receipt timestamp used by the
    watchdog to evaluate progress windows.  Pass a ``datetime`` or ISO string.
    When absent, falls back to ``t`` for backwards compatibility.
    """
    try:
        if not isinstance(metric, str) or not _PROGRESS_METRIC_KEY_RE.fullmatch(metric):
            return meta
        float_val = float(value)
        if not math.isfinite(float_val):
            return meta
        if not isinstance(t, str) or not t:
            return meta
        meta = dict(meta)
        timestamp = _parse_naive_iso_datetime(t)
        series: list = [item for item in list(meta.get("progress_series") or []) if isinstance(item, dict)]
        # Keep at most 16 metric names, reserving a slot for a newly declared
        # metric; the configured metric is always retained.
        metric_names = {item.get("name") for item in series if isinstance(item.get("name"), str)}
        if metric not in metric_names and len(metric_names) >= _PROGRESS_SERIES_MAX_METRICS:
            declared = meta.get("declared_metric")
            removable = next((name for name in metric_names if name != declared), None)
            if removable is None:
                return meta
            series = [item for item in series if item.get("name") != removable]
        bucket = (
            int(timestamp.timestamp()) // _PROGRESS_SAMPLE_BUCKET_SECONDS
            if timestamp is not None
            else None
        )
        # A server can receive many reports in one minute.  Replacing the bucket's
        # point retains the latest observed value without allowing report rate to
        # shrink the retained time horizon.
        series = [
            item for item in series
            if not (
                item.get("name") == metric
                and bucket is not None
                and (parsed := _parse_naive_iso_datetime(item.get("t"))) is not None
                and int(parsed.timestamp()) // _PROGRESS_SAMPLE_BUCKET_SECONDS == bucket
            )
        ]
        received_at_str: str | None
        if isinstance(received_at, datetime):
            received_at_str = received_at.isoformat()
        elif isinstance(received_at, str) and received_at:
            received_at_str = received_at
        else:
            received_at_str = t
        series.append({"t": t, "received_at": received_at_str, "name": metric, "value": float_val})
        metric_series = [item for item in series if item.get("name") == metric]
        if len(metric_series) > _PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC:
            keep = {id(item) for item in metric_series[-_PROGRESS_SERIES_MAX_SAMPLES_PER_METRIC:]}
            series = [item for item in series if item.get("name") != metric or id(item) in keep]
        meta["progress_series"] = series
        return meta
    except Exception:  # noqa: BLE001 - telemetry ingestion must remain fail-open
        return meta


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

# Default terminal_kind label per outcome, used when the caller does not
# supply an explicit terminal_kind to _finalize_terminal_job.
_OUTCOME_TO_TERMINAL_KIND: dict[ComputeJobOutcome, str | None] = {
    ComputeJobOutcome.SUCCESS: None,
    ComputeJobOutcome.CANCELLED: "operator",
    ComputeJobOutcome.TIMED_OUT: "deadline",
    ComputeJobOutcome.SPEND_CAP_EXCEEDED: "spend_cap",
    ComputeJobOutcome.FAILURE: "failure",
    ComputeJobOutcome.SMOKE_FAILED: "failure",
}


class ComputeLaunchPreconditionFailed(Exception):
    """Structured launch precondition failure safe to return to agents."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("message") or detail.get("error") or detail))


def _compute_job_has_durable_bundle(job: ComputeJob) -> bool:
    """True when a compute job actually produced a durable artifact bundle.

    A cross-job resume source must have a downloadable bundle. ``result_reference``
    / ``artifact_id`` are stamped by ``_link_terminal_artifact`` only after a real
    bundle is linked, and runner-subprocess jobs additionally record a
    ``runner_artifact`` descriptor. Any of these signals means a ``bundle.tar.gz``
    exists under the job's per-job artifact prefix.
    """
    if job.result_reference or job.artifact_id is not None:
        return True
    runner_artifact = (job.polling_metadata or {}).get("runner_artifact")
    return isinstance(runner_artifact, dict) and bool(runner_artifact.get("uri"))


async def _resolve_resume_source_job_id(
    session, *, project_id, input_artifact_job_id: str
) -> str:
    """Validate a cross-job checkpoint-resume source and return its canonical id.

    Rejects (raising :class:`ComputeLaunchPreconditionFailed`, mapped to an
    agent-facing error) a malformed uuid, a job that does not exist, a job owned
    by a DIFFERENT project, or a job that never produced a durable artifact
    bundle. Fail-closed by contract: a dangling / foreign / empty resume reference
    must never silently no-op — that would recreate the fail-open reproducibility
    dishonesty this project is fighting.
    """
    raw = str(input_artifact_job_id or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "invalid_input_artifact_job_id",
                "message": f"input_artifact_job_id is not a valid UUID: {raw!r}",
                "input_artifact_job_id": raw,
            }
        ) from exc
    source = await session.get(ComputeJob, parsed)
    if source is None:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "input_artifact_job_not_found",
                "message": (
                    f"input_artifact_job_id {parsed} does not reference an "
                    "existing compute job"
                ),
                "input_artifact_job_id": str(parsed),
            }
        )
    if source.project_id != project_id:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "input_artifact_job_cross_project",
                "message": (
                    "input_artifact_job_id belongs to a different project — "
                    "cross-project checkpoint resume is refused"
                ),
                "input_artifact_job_id": str(parsed),
            }
        )
    _source_status = (
        source.status
        if isinstance(source.status, ComputeJobStatus)
        else ComputeJobStatus(source.status)
    )
    if _source_status not in _COMPUTE_JOB_TERMINAL_STATUSES:
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "input_artifact_job_not_terminal",
                "message": (
                    f"input_artifact_job_id {parsed} is still {_source_status.value} — "
                    "resuming from a non-terminal job risks rehydrating a "
                    "partial/mid-checkpoint bundle"
                ),
                "input_artifact_job_id": str(parsed),
                "source_status": _source_status.value,
            }
        )
    if not _compute_job_has_durable_bundle(source):
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "input_artifact_job_has_no_bundle",
                "message": (
                    f"input_artifact_job_id {parsed} produced no durable artifact "
                    "bundle (no result_reference / artifact / runner_artifact) — "
                    "nothing to resume from"
                ),
                "input_artifact_job_id": str(parsed),
            }
        )
    source_meta = source.polling_metadata or {}
    if source_meta.get("input_artifact_job_id") is not None:
        provenance = source_meta.get("resume_provenance")
        valid_provenance = (
            isinstance(provenance, dict)
            and provenance.get("ok") is True
            and str(provenance.get("resumed_from_job_id") or "")
            == str(source_meta.get("input_artifact_job_id"))
            and isinstance(provenance.get("file_count"), int)
            and provenance["file_count"] > 0
        )
        if not valid_provenance:
            raise ComputeLaunchPreconditionFailed(
                {
                    "error": "input_artifact_job_resume_provenance_invalid",
                    "message": (
                        "input_artifact_job_id has incomplete or failed resume "
                        "hydration provenance and cannot be used as a checkpoint ancestor"
                    ),
                    "input_artifact_job_id": str(parsed),
                }
            )
    return str(parsed)


def _has_verified_resume_provenance(job: Any) -> bool:
    """Return True when *job* carries a valid cross-job checkpoint provenance.

    A provenance is valid when it was written by a successful hydration run on
    the same predecessor job: ok=True, resumed_from_job_id matches the job's
    declared input_artifact_job_id, and at least one file was copied.  This is
    the same contract enforced by ``_validate_input_artifact_job_id`` but
    expressed as a boolean predicate for use in tests and status checks.
    """
    meta = (job.polling_metadata or {}) if hasattr(job, "polling_metadata") else {}
    input_job_id = meta.get("input_artifact_job_id")
    if input_job_id is None:
        return False
    provenance = meta.get("resume_provenance")
    return (
        isinstance(provenance, dict)
        and provenance.get("ok") is True
        and str(provenance.get("resumed_from_job_id") or "") == str(input_job_id)
        and isinstance(provenance.get("file_count"), int)
        and provenance["file_count"] > 0
    )


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
    allow_host_fallback: bool = False,
    no_progress_timeout_seconds: int | None = None,
    declared_metric: str | None = None,
    input_artifact_job_id: str | None = None,
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
    # Validate and clamp no_progress_timeout_seconds: positive, not exceeding the
    # runtime deadline, and never shorter than 60s (a shorter window would false-
    # fire on normal compute startup / dependency install).
    capped_np_timeout: int | None = None
    if no_progress_timeout_seconds is not None:
        raw_np = int(no_progress_timeout_seconds)
        if raw_np < 0:
            raise ValueError(
                f"no_progress_timeout_seconds must be positive (got {raw_np})"
            )
        if raw_np == 0:
            capped_np_timeout = None  # 0 = disable
        else:
            capped_np_timeout = max(60, min(raw_np, capped_deadline))
    if (
        input_artifact_job_id is not None
        and str(input_artifact_job_id).strip()
        and capped_np_timeout is None
    ):
        raise ComputeLaunchPreconditionFailed(
            {
                "error": "input_artifact_watchdog_required",
                "message": (
                    "input_artifact_job_id requires an explicit positive "
                    "no_progress_timeout_seconds watchdog"
                ),
                "input_artifact_job_id": str(input_artifact_job_id),
            }
        )
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
    runner_hostname: str | None = (locked_task.context or {}).get("runner_hostname") or None
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
    # Cross-job checkpoint resume: validate the resume source BEFORE persisting the
    # row so a bad reference is rejected at launch (agent-facing error) rather than
    # silently no-op'ing at hydration time. Same-project + durable-bundle enforced.
    resolved_input_artifact_job_id: str | None = None
    if input_artifact_job_id is not None and str(input_artifact_job_id).strip():
        resolved_input_artifact_job_id = await _resolve_resume_source_job_id(
            session,
            project_id=locked_task.project_id,
            input_artifact_job_id=input_artifact_job_id,
        )
        if capped_np_timeout is None or not (isinstance(declared_metric, str) and declared_metric.strip()):
            raise ComputeLaunchPreconditionFailed(
                {
                    "error": "resume_requires_watchdog",
                    "message": (
                        "resumed compute jobs require declared_metric and a positive "
                        "no_progress_timeout_seconds so stalled checkpoint runs are cut"
                    ),
                }
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
        no_progress_timeout_seconds=capped_np_timeout,
        input_artifact_job_id=resolved_input_artifact_job_id,
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
        "status_message": _LAUNCH_PLACEHOLDER_STATUS_MESSAGE,
        "park_deadline_seconds": park_deadline_seconds,
    }
    if isinstance(declared_metric, str) and declared_metric.strip():
        # Persisted so the no-progress watchdog knows which reported metric to
        # watch for a stall. Absent → the watchdog never fires for this job.
        polling_metadata["declared_metric"] = declared_metric.strip()
    if setup_step is not None:
        polling_metadata["setup_step"] = setup_step
    if capped_np_timeout is not None:
        polling_metadata["no_progress_timeout_seconds"] = capped_np_timeout
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
                registry_meta["password"] = encrypt_field(str(container_registry_auth["password"]))
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
    if runner_hostname is not None:
        polling_metadata["runner_hostname"] = runner_hostname
    if allow_host_fallback:
        polling_metadata["allow_host_fallback"] = True
    if resolved_input_artifact_job_id is not None:
        # Durable launch input (like command / container_image): the resume source
        # whose checkpoint bundle is hydrated into $BLUEPRINT_ARTIFACT_DIR before
        # the command runs. Recorded so the operator can see the run resumed and
        # from where, and so _build_launch_spec can forward it to the backend.
        polling_metadata["input_artifact_job_id"] = resolved_input_artifact_job_id
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
    no_progress_timeout_seconds: int | None = None,
    input_artifact_job_id: str | None = None,
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
        "no_progress_timeout_seconds": no_progress_timeout_seconds,
        "input_artifact_job_id": input_artifact_job_id,
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


def _declared_progress_metric(job: ComputeJob) -> str | None:
    """Return the contract-declared metric name to watch, or None.

    Written into ``polling_metadata['declared_metric']`` at job creation from the
    validated compute_experiment launch contract. Absent for jobs that declare no
    metric (the watchdog then never fires for them).
    """
    metric = (job.polling_metadata or {}).get("declared_metric")
    if isinstance(metric, str) and metric.strip():
        return metric
    return None


def _watched_progress_samples(
    job: ComputeJob, metric: str
) -> list[tuple[datetime, float]]:
    """Return receipt-time-sorted samples for the declared metric.

    ``received_at`` is written by the authenticated ingestion route and is the
    preferred enforcement clock. When ``received_at`` is *absent* (pre-upgrade
    samples stored before the field was introduced), the runner-authored ``t``
    field is used as a fallback so that a rolling deploy cannot convert a
    healthy in-flight job into TIMED_OUT. Samples where ``received_at`` is
    *present but malformed* are still skipped.
    """
    series = (job.polling_metadata or {}).get("progress_series")
    if not isinstance(series, list):
        return []
    out: list[tuple[datetime, float]] = []
    for sample in series:
        if not isinstance(sample, dict) or sample.get("name") != metric:
            continue
        if "received_at" in sample:
            ts = _parse_naive_iso_datetime(sample["received_at"])
        else:
            # Pre-upgrade sample: received_at was never written; fall back to
            # the runner-authored timestamp so the sample is not silently dropped.
            ts = _parse_naive_iso_datetime(sample.get("t"))
        val = _coerce_metric_number(sample.get("value"))
        if ts is None or val is None:
            continue
        out.append((ts, float(val)))
    out.sort(key=lambda pair: pair[0])
    return out


def _watchdog_metric_name_mismatch(job: ComputeJob) -> list[str]:
    """Return the observed progress-metric names when the no-progress watchdog is
    INERT purely because of a metric-name mismatch, else ``[]``.

    The watchdog keys on exact name equality between the contract-declared metric
    (:func:`_declared_progress_metric`) and the ``name`` of each recorded progress
    sample (:func:`_watched_progress_samples`). A job that declares metric ``X`` but
    only ever reports progress under some other name ``Y`` accumulates ZERO samples
    for ``X``, so the watchdog can never fire — the waste-prevention lever the
    operator explicitly asked for is SILENTLY inert. This predicate detects exactly
    that case so the poller can surface it honestly (a durable marker + a warning
    log) instead of the lever failing silent.

    Returns the sorted, de-duplicated set of metric names actually present in the
    bounded progress history when ALL of the following hold; otherwise ``[]``:

    * the job declares a metric to watch;
    * the history holds at least one well-formed sample (the job IS reporting
      progress, so a genuinely silent job — no telemetry at all — is never flagged;
      that case is the watchdog's normal fail-open domain, not a name mismatch); and
    * none of those samples carry the declared metric's name.
    """
    metric = _declared_progress_metric(job)
    if metric is None:
        return []
    series = (job.polling_metadata or {}).get("progress_series")
    if not isinstance(series, list):
        return []
    observed: set[str] = set()
    for sample in series:
        if not isinstance(sample, dict):
            continue
        name = sample.get("name")
        if not isinstance(name, str) or not name:
            continue
        if _coerce_metric_number(sample.get("value")) is None:
            continue
        observed.add(name)
    if not observed or metric in observed:
        return []
    return sorted(observed)


def _no_progress_stalled(job: ComputeJob, *, now: datetime) -> bool:
    """True when a declared-metric job has stopped making measurable progress.

    Fail-open: returns False unless ALL of the following hold, so a job that emits
    no progress telemetry is never affected —

    * the job declares a metric to watch;
    * the job has been running longer than the grace period (never cut a job
      before it has had a fair chance to start converging);
    * the metric history spans at least a full window (the oldest recorded
      sample is at least ``WINDOW`` seconds old) — so a job that has only just
      begun reporting is never cut on a few seconds of data;
    * at least ``COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES`` samples of that metric fall
      inside the trailing ``[now - WINDOW, now]`` window (a stale buffer whose
      last sample predates the window contributes nothing and cannot fire); and
    * the spread (max - min) of the metric across that window is within a small
      relative tolerance of the metric's magnitude — a sustained plateau.

    Direction-agnostic: a flat metric is "no progress" whether higher or lower
    is better, so the check never needs to know the metric's polarity.

    Per-job override: if ``no_progress_timeout_seconds`` is stored in the job's
    ``polling_metadata`` (set at job-creation time from the experiment contract),
    it is used as both the grace period and the plateau-detection window instead
    of the global env-var defaults.
    """
    meta = job.polling_metadata or {}
    raw_per_job = meta.get("no_progress_timeout_seconds")
    if raw_per_job is not None:
        try:
            per_job_seconds = float(raw_per_job)
        except (TypeError, ValueError):
            per_job_seconds = None
    else:
        per_job_seconds = None

    window_seconds = per_job_seconds if per_job_seconds is not None else COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS
    grace_seconds = per_job_seconds if per_job_seconds is not None else COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS

    if window_seconds <= 0:
        return False
    metric = _declared_progress_metric(job)
    if metric is None:
        return False
    if job.started_at is None:
        return False
    if (now - job.started_at).total_seconds() < grace_seconds:
        return False
    samples = _watched_progress_samples(job, metric)
    if not samples:
        # Reporting under another name is a contract mismatch, not silence.  It
        # is surfaced as unprotected and must not be mistaken for a missing signal.
        if _watchdog_metric_name_mismatch(job):
            return False
        # A declared watchdog is an enforcement promise, not merely telemetry
        # decoration: after its grace window, missing telemetry is no progress.
        return True
    window_start = now - timedelta(seconds=window_seconds)
    if samples[-1][0] < window_start:
        return True
    if len(samples) < COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES:
        return False
    # Enough history: the oldest recorded sample must be at least a full window
    # old, else we have not yet observed a window's worth of the metric.
    if (now - samples[0][0]).total_seconds() < window_seconds:
        return False
    window = [(t, v) for (t, v) in samples if t >= window_start]
    if len(window) < COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES:
        return False
    values = [v for (_, v) in window]
    spread = max(values) - min(values)
    reference = max((abs(v) for v in values), default=0.0)
    tolerance = COMPUTE_JOB_NO_PROGRESS_EPSILON * (reference if reference > 0 else 1.0)
    return spread <= tolerance


def derive_watchdog_protection_status(
    polling_metadata: dict | None,
    *,
    started_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Derive watchdog-protection status fields from a job's polling_metadata.

    Intended for inclusion in serialized compute-job response payloads.  The
    derivation mirrors ``_no_progress_stalled`` exactly so operators see the same
    effective timeout the watchdog would use — there is no forked definition.

    Returns a dict with:
      watchdog_state (str): High-level state — one of 'protected', 'warming_up',
          'insufficient_history', 'stale_telemetry', 'inert'.
      watchdog_active (bool): True only when watchdog_state == 'protected'.
      no_progress_timeout_seconds (int|None): Effective timeout in seconds
          (per-job override if set, else global default). None when no metric declared.
      last_progress_at (str|None): ISO timestamp of the most recent
          progress_series sample matching the declared metric (server receipt time),
          or None.
      watchdog_inert_reason (str|None): Granular reason the watchdog is not
          firing. None when active.
    """
    from types import SimpleNamespace

    _now = now if now is not None else _utcnow()

    meta = polling_metadata or {}
    _job = SimpleNamespace(polling_metadata=meta)

    # Normalise started_at: accept datetime or ISO string from JSON-serialised payloads.
    _started_at: datetime | None = None
    if isinstance(started_at, datetime):
        _started_at = started_at
    elif isinstance(started_at, str) and started_at:
        _started_at = _parse_naive_iso_datetime(started_at)

    # Resolve effective timeout — identical to the resolution in _no_progress_stalled.
    raw_per_job = meta.get("no_progress_timeout_seconds")
    per_job_seconds: float | None = None
    if raw_per_job is not None:
        try:
            per_job_seconds = float(raw_per_job)
        except (TypeError, ValueError):
            pass
    effective_seconds = (
        per_job_seconds if per_job_seconds is not None else COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS
    )
    grace_seconds = (
        per_job_seconds if per_job_seconds is not None else COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS
    )

    metric = _declared_progress_metric(_job)

    if metric is None:
        return {
            "watchdog_state": "inert",
            "watchdog_active": False,
            "no_progress_timeout_seconds": None,
            "last_progress_at": None,
            "watchdog_inert_reason": "no_declared_metric",
        }

    if effective_seconds <= 0:
        return {
            "watchdog_state": "inert",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": None,
            "watchdog_inert_reason": "timeout_disabled",
        }

    # Collect matching samples using server receipt time (received_at).
    series = meta.get("progress_series")
    last_received_at: str | None = None
    last_received_ts: datetime | None = None
    has_any_sample = False
    has_valid_received_at = False

    if isinstance(series, list):
        for sample in series:
            if not isinstance(sample, dict) or sample.get("name") != metric:
                continue
            has_any_sample = True
            recv_str = sample.get("received_at")
            ts = _parse_naive_iso_datetime(recv_str)
            if ts is None:
                continue
            has_valid_received_at = True
            if last_received_ts is None or ts > last_received_ts:
                last_received_ts = ts
                last_received_at = recv_str

    # Metric-name mismatch is deterministic: samples exist for OTHER metrics
    # but none match the declared one.  Surface this before grace/start-time checks
    # because it is always inert regardless of when the job started.
    if not has_any_sample and _watchdog_metric_name_mismatch(_job):
        return {
            "watchdog_state": "inert",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": None,
            "watchdog_inert_reason": "metric_name_mismatch",
        }

    # When started_at is absent we cannot evaluate the grace period — surface that
    # honestly as insufficient_history/missing_start_time.
    if _started_at is None:
        return {
            "watchdog_state": "insufficient_history",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": last_received_at,
            "watchdog_inert_reason": "missing_start_time",
        }
    if (_now - _started_at).total_seconds() < grace_seconds:
        return {
            "watchdog_state": "warming_up",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": last_received_at,
            "watchdog_inert_reason": None,
        }

    # No samples for the declared metric (and no mismatch detected above).
    if not has_any_sample:
        return {
            "watchdog_state": "insufficient_history",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": None,
            "watchdog_inert_reason": "no_telemetry",
        }

    # Samples exist for the declared metric but none have a valid received_at.
    if not has_valid_received_at:
        return {
            "watchdog_state": "insufficient_history",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": None,
            "watchdog_inert_reason": "malformed_history",
        }

    # All samples are older than the effective window — stale telemetry.
    window_start = _now - timedelta(seconds=effective_seconds)
    if last_received_ts is not None and last_received_ts < window_start:
        return {
            "watchdog_state": "stale_telemetry",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": last_received_at,
            "watchdog_inert_reason": "stale_telemetry",
        }

    # Samples exist and are fresh — check whether we have a full window of history.
    samples = _watched_progress_samples(_job, metric)
    if len(samples) < COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES:
        return {
            "watchdog_state": "insufficient_history",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": last_received_at,
            "watchdog_inert_reason": "insufficient_window",
        }
    if samples and ((_now - samples[0][0]).total_seconds() < effective_seconds):
        return {
            "watchdog_state": "insufficient_history",
            "watchdog_active": False,
            "no_progress_timeout_seconds": int(effective_seconds),
            "last_progress_at": last_received_at,
            "watchdog_inert_reason": "insufficient_window",
        }

    return {
        "watchdog_state": "protected",
        "watchdog_active": True,
        "no_progress_timeout_seconds": int(effective_seconds),
        "last_progress_at": last_received_at,
        "watchdog_inert_reason": None,
    }


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


def _status_is_no_progress_timeout(status: ComputeStatusResult) -> bool:
    """True for every backend's reserved no-progress terminal signal."""
    return (
        status.exit_code == 125
        or "no_progress_timeout" in (status.message or "").strip().lower()
    )


# Fatal "no usable GPU" diagnostics printed by container entrypoints (notably
# the NGC / ``nvcr.io/nvidia/pytorch`` images) when the host GPU predates the
# image's minimum CUDA compute capability — e.g. a Tesla V100 (Volta, CC 7.0)
# under a container built for Turing+. The entrypoint prints the error and can
# still exit 0, so the backend reports SUCCEEDED even though the command never
# had a usable GPU and produced no metric. Matching these markers lets the
# poller reclassify the run as a FAILURE so a GPU/image mismatch is diagnosed
# loudly instead of masquerading as a clean, metric-less success.
_NO_USABLE_GPU_LOG_MARKERS: tuple[str, ...] = (
    "no supported gpu(s) detected to run this container",
    "gpu, which is not supported by this container",
    # CUDA error 209: image lacks device code for the host GPU arch (e.g. sm_70/Volta)
    "no kernel image is available for execution on the device",
)
_NO_USABLE_GPU_TERMINAL_REASON = (
    "container reported no usable GPU — host GPU/image compatibility mismatch"
)

# A metric-or-arms job that exits 0 without fulfilling its declared result
# contract produced no usable result — the command silently no-op'd (crashed
# before writing its result marker, mis-declared its metric, wrote to the wrong
# path, etc.) yet the backend reports SUCCEEDED. Incidental durable files (logs
# and checkpoints) are preserved and linked, but are not evidence that the
# declared metric or per-arm result was delivered. Detecting the unfulfilled
# terminal lets the poller reclassify it as a FAILURE so the missing result
# surfaces loudly. The arms sweep path is especially vulnerable: its pre-flight
# metric assertion is deliberately skipped (see _run_and_apply_preflight_smoke:
# declared_metric_for_smoke is None for arms).
_NO_DATA_TERMINAL_REASON = (
    "compute job exited 0 but never measured its declared result — no usable result"
)
_NO_CRITERION_EXPERIMENT_REASON = (
    "compute_experiment declared no metric and produced no measurements — "
    "a metric or success_criterion is required for experiments"
)

# Seeded into polling_metadata["status_message"] at job-creation time (see the
# launch metadata builder). It is a launch-time placeholder, NOT a terminal
# diagnostic — a terminal reason must never fall back to it, or completed and
# failed runs alike get recorded with this misleading "still queued" string.
_LAUNCH_PLACEHOLDER_STATUS_MESSAGE = "queued for compute-job launch"


def _honest_terminal_default_reason(outcome: "ComputeJobOutcome | None") -> str:
    """An honest per-outcome terminal reason for when the backend is silent.

    Used when a job reaches a terminal state but the backend poll returns an
    empty status message, so the terminal_reason still reflects how the run
    actually ended instead of a generic/stale placeholder.
    """
    if outcome == ComputeJobOutcome.SUCCESS:
        return "compute job succeeded"
    if outcome == ComputeJobOutcome.TIMED_OUT:
        return "compute job timed out"
    if outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED:
        return "compute job terminated at spend cap"
    if outcome == ComputeJobOutcome.CANCELLED:
        return "compute job cancelled"
    if outcome in (ComputeJobOutcome.FAILURE, ComputeJobOutcome.SMOKE_FAILED):
        return "compute job reported failure"
    return "compute job finished"


def _resolve_terminal_reason(
    *,
    status_message: str | None,
    outcome: "ComputeJobOutcome | None",
) -> str:
    """Resolve an honest terminal reason for a job reaching a terminal state.

    Precedence: the terminal poll's own status message, then an honest
    per-outcome default. It deliberately does NOT fall back to the persisted
    ``polling_metadata["status_message"]``: that field only ever holds a
    launch/intermediate value — the launch-time placeholder
    ("queued for compute-job launch"), the launch result message, or a
    provisioning-retry message — none of which is a terminal reason. Inheriting
    it (as the previous ``_meta_message`` fallback did) recorded completed and
    failed runs alike with a misleading "still queued"/"launched" diagnostic
    that then propagated verbatim into the durable knowledge/evidence record.
    """
    if status_message:
        return status_message
    return _honest_terminal_default_reason(outcome)


def _status_reports_no_usable_gpu(status: ComputeStatusResult) -> bool:
    """True when the command log carries a fatal no-usable-GPU diagnostic.

    Some container entrypoints print a fatal "No supported GPU(s) detected" /
    "GPU, which is not supported by this container" message when the host GPU is
    too old for the image's CUDA build, yet may still exit 0 — in which case the
    backend reports SUCCEEDED. Detecting the marker lets the poller reclassify
    the run as a FAILURE instead of a silent, metric-less success.
    """
    command_log = status.command_log
    if not isinstance(command_log, dict):
        return False
    text = command_log.get("text")
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _NO_USABLE_GPU_LOG_MARKERS)


def _resolved_metric_names(
    job: ComputeJob, status: ComputeStatusResult
) -> set[str]:
    """Metric names measurable from a job's FULLY-resolved terminal metric set.

    Mirrors exactly what :func:`_apply_polled_compute_job_result` will persist and
    what :func:`build_compute_result_logs` (which the success-criterion gate reads)
    then resolves, so this guard sees the same names the gate will:

    * ``result_metrics`` — the current poll's ``status.metrics`` when non-empty,
      else the value a PRIOR poll persisted (``_apply_polled_compute_job_result``
      only overwrites ``result_metrics`` on a truthy ``status.metrics``, so a metric
      captured earlier stays resolvable — matching the sticky persistence and
      preventing a false-positive FAILURE on a final metric-less poll);
    * the per-arm block used by the best-arm evaluator (any numeric key inside any
      arm of ``result_metrics['arms']``); and
    * the stdout JSON fallback — but only when ``result_metrics`` is absent/empty,
      the exact condition under which the artifact builder consults it.

    Defensive: tolerates missing/malformed metadata and only ever counts values
    that coerce to a real number, matching how the evaluator decides ``measurable``.
    """
    meta = job.polling_metadata or {}
    result_metrics = (
        dict(status.metrics)
        if isinstance(status.metrics, dict) and status.metrics
        else meta.get("result_metrics")
    )
    result_metrics = result_metrics if isinstance(result_metrics, dict) else {}
    command_log = (
        dict(status.command_log)
        if isinstance(status.command_log, dict) and status.command_log
        else meta.get("command_log")
    )
    names: set[str] = set()
    for key, value in result_metrics.items():
        if key == "arms":
            continue
        if _coerce_metric_number(value) is not None:
            names.add(str(key))
    arms = result_metrics.get("arms")
    if isinstance(arms, dict):
        for arm in arms.values():
            if isinstance(arm, dict):
                for key, value in arm.items():
                    if _coerce_metric_number(value) is not None:
                        names.add(str(key))
    # stdout fallback fires ONLY when result-file metrics are absent/empty — the
    # same gate build_compute_result_logs applies before scanning stdout.
    if not result_metrics:
        cl = {"command_log": command_log} if isinstance(command_log, dict) else {}
        fallback = _stdout_metrics_fallback(cl)
        if isinstance(fallback, dict):
            for key, value in fallback.items():
                if _coerce_metric_number(value) is not None:
                    names.add(str(key))
    return names


def _declared_metric_unmeasured(job: ComputeJob, status: ComputeStatusResult) -> bool:
    """True when ``job`` declared a metric its terminal result never captured.

    The general form of the ``_status_reports_no_usable_gpu`` special case: a
    backend-reported SUCCEEDED that declared a metric (from the compute_experiment
    launch contract / success_criterion) but produced no value for it — after all
    existing metric resolution (result file, per-arm block, stdout fallback) — did
    not actually measure its hypothesis and must not record a clean SUCCESS.

    Fail-safe, mirroring the watchdog and the no-usable-GPU guard: returns False
    when the job declared no metric (metric-less jobs are byte-for-byte unaffected)
    or when the declared metric IS present in the fully-resolved set. Comparison is
    case-insensitive to match the success-criterion evaluator's case-insensitive
    metric lookup.
    """
    metric = _declared_progress_metric(job)
    if metric is None:
        return False
    resolved = {name.lower() for name in _resolved_metric_names(job, status)}
    return metric.strip().lower() not in resolved


def _status_has_no_reported_metrics(status: ComputeStatusResult) -> bool:
    """True when a terminal status carries no self-reported result metrics.

    The launched command reports its metrics as a small flat JSON object via the
    result marker; an absent, non-dict, or empty ``metrics`` means the run
    produced no evaluable metric — the "no data" precondition for the guard.
    """
    metrics = status.metrics
    return not isinstance(metrics, dict) or not metrics


def _task_declares_arms(task: Task | None) -> bool:
    """True when the task contract requires a per-arm result block."""
    return bool((task.first_payload or {}).get("arms")) if task is not None else False


def _arms_have_reported_data(job: ComputeJob, status: ComputeStatusResult) -> bool:
    """Return whether an arms contract has any numeric per-arm result.

    Use the same current-poll-or-sticky result source as the terminal metric
    resolver. A flat metric or incidental artifact cannot fulfill an arms
    contract: the success evaluator needs a numeric value inside an arm block.
    """
    result_metrics = (
        status.metrics
        if isinstance(status.metrics, dict) and status.metrics
        else (job.polling_metadata or {}).get("result_metrics")
    )
    arms = result_metrics.get("arms") if isinstance(result_metrics, dict) else None
    return isinstance(arms, dict) and any(
        isinstance(arm, dict)
        and any(_coerce_metric_number(value) is not None for value in arm.values())
        for arm in arms.values()
    )


def _no_data_terminal_reason(artifact_reference: Any | None) -> str:
    """Explain an unfulfilled result contract without discarding artifacts."""
    if artifact_reference is None:
        return _NO_DATA_TERMINAL_REASON
    return (
        f"{_NO_DATA_TERMINAL_REASON}; durable artifacts preserved "
        f"(uri={artifact_reference.uri}, file_count={artifact_reference.file_count})"
    )


def _job_expects_data(job: ComputeJob, task: Task | None) -> bool:
    """True when the job declared it should produce a metric or arms output.

    A pure side-effect command (no declared metric, no arms sweep) can legitimately
    finish with neither metrics nor durable artifacts, so the no-data guard must
    NOT fire for it — that would false-reject a genuinely successful run. Only jobs
    that declared a metric (``polling_metadata['declared_metric']`` stamped at job
    creation, or the task payload's ``declared_metric`` / ``success_criterion.metric``)
    or an ``arms`` block are expected to emit data; those finishing empty are the
    silent-failure case the guard catches. Mirrors the declared-metric/arms
    derivation in :func:`_run_and_apply_preflight_smoke` and reads the same
    ``polling_metadata['declared_metric']`` the no-progress watchdog keys on.
    """
    if (job.polling_metadata or {}).get("declared_metric"):
        return True
    if task is None:
        return False
    payload = task.first_payload or {}
    if payload.get("arms"):
        return True
    criterion = payload.get("success_criterion")
    declared_metric = payload.get("declared_metric") or (
        criterion.get("metric") if isinstance(criterion, dict) else None
    )
    return bool(declared_metric)


def _is_criterion_free_experiment(job: "ComputeJob", task: Task | None) -> bool:
    """True for compute_experiment tasks that declared no metric and no criterion.

    A compute_experiment is a measurement task by definition — finishing with no
    declared metric, no arms, and no success_criterion is never a clean SUCCESS.
    The run produced nothing evaluable, so it must be reclassified as FAILURE rather
    than silently benefiting from the _job_expects_data carve-out that was designed
    for pure side-effect commands. Non-experiment task types (research, plan, …) are
    unaffected: they are legitimate side-effect jobs and the carve-out stays intact.

    Checks both the task payload AND the job's polling_metadata for a stamped
    declared_metric so a job created with an explicit declared_metric kwarg (which
    stamps polling_metadata["declared_metric"] at creation time) is never
    misclassified as criterion-free.
    """
    if task is None:
        return False
    payload = task.first_payload or {}
    if str(payload.get("type") or "") != "compute_experiment":
        return False
    # Declared metric stamped in polling_metadata at job creation → not criterion-free.
    if (job.polling_metadata or {}).get("declared_metric"):
        return False
    # Already has a declared metric or arms in payload — handled by the existing path.
    if payload.get("declared_metric") or payload.get("arms"):
        return False
    # Has a success_criterion (metric or artifact_produced) — a well-defined contract.
    if payload.get("success_criterion") is not None:
        return False
    return True


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
    _record_terminal_reason_history(session, live)
    await _link_terminal_artifact(session, live)
    await persist_viewable_files_for_job(session, live)
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


async def finalize_silent_compute_job(
    session, job: ComputeJob, *, now
) -> bool:
    """Drive a silent runner-subprocess or GCP job to TIMED_OUT.

    Re-locks and re-checks heartbeat staleness before finalizing. GCP jobs are
    finalized only after successful VM deletion; failed deletes retain RUNNING
    state with a durable diagnostic for retry.
    """
    # Rollback expires every ORM instance in the session.  Keep the identifier
    # as a plain scalar so the GCP terminate error path can persist its
    # diagnostic without touching either ``job`` or ``live`` after rollback.
    job_id = job.id
    live = await _lock_compute_job(session, job_id)
    if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        await session.commit()
        return False
    if live.status != ComputeJobStatus.RUNNING or live.backend not in (
        RUNNER_SUBPROCESS_BACKEND_NAME,
        GCP_BACKEND_NAME,
    ):
        await session.commit()
        return False

    # Re-check heartbeat staleness under the lock.  A concurrent /report
    # (which refreshes last_heartbeat_at while keeping the status RUNNING)
    # may have committed between the initial SELECT (without FOR UPDATE)
    # and this lock.  With populate_existing=True the row above reflects
    # the latest committed state: if the heartbeat is now fresh the job is
    # still healthy and must not be terminalized.
    stale_cutoff = now - timedelta(seconds=RUNNER_SILENT_TIMEOUT_SECONDS)
    hb = live.last_heartbeat_at
    if hb is not None and hb >= stale_cutoff:
        await session.commit()
        return False
    # GCP RUNNING means the first heartbeat was received; provisioning owns the
    # no-first-heartbeat case.
    if live.backend == GCP_BACKEND_NAME and hb is None:
        await session.commit()
        return False
    if (
        live.backend == RUNNER_SUBPROCESS_BACKEND_NAME
        and hb is None
        and (live.started_at is None or live.started_at >= stale_cutoff)
    ):
        await session.commit()
        return False

    terminal_reason = (
        "vm_died_silently"
        if live.backend == GCP_BACKEND_NAME
        else "runner_died_silently"
    )
    if live.backend == GCP_BACKEND_NAME:
        terminate_started_at = _utcnow()
        try:
            terminated = await get_compute_backend(live.backend).terminate(
                live, reason="heartbeat_stale"
            )
            if not terminated:
                raise RuntimeError("GCP VM delete did not complete")
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] stale-heartbeat terminate failed for GCP job=%s",
                live.id,
            )
            await session.rollback()
            await _persist_compute_rpc_failure(
                session,
                job_id=job_id,
                operation="terminate",
                exc=exc,
                started_at=terminate_started_at,
                failed_at=failed_at,
                next_retry_at=None,
                reason="heartbeat_stale",
            )
            return False

    _finalize_terminal_job(
        live,
        status=ComputeJobStatus.TIMED_OUT,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason=terminal_reason,
        now=now,
        terminal_kind=terminal_reason,
    )
    _record_terminal_reason_history(session, live)
    await _link_terminal_artifact(session, live)
    await persist_viewable_files_for_job(session, live)
    if live.task_id is not None:
        from app.task_lifecycle import fire_task_wakes

        await fire_task_wakes(
            session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(live.id),
        )
    await session.commit()
    return True


async def finalize_orphan_runner_job(session, job: ComputeJob, *, now) -> bool:
    """Backward-compatible name for runner-only callers."""
    return await finalize_silent_compute_job(session, job, now=now)


async def list_compute_jobs(
    session,
    *,
    project_id: uuid.UUID,
    include_finished_within: timedelta = timedelta(hours=6),
    limit: int = 50,
    offset: int = 0,
    all_terminal: bool = False,
    task_id: uuid.UUID | None = None,
) -> tuple[list[ComputeJob], bool]:
    """Return a project's active compute jobs plus recently-finished ones.

    Active jobs are always included. When ``all_terminal=True``, ALL terminal
    jobs are included regardless of age (``include_finished_within`` is ignored
    for the terminal filter). Otherwise only terminal jobs completed within
    ``include_finished_within`` are returned. Ordered newest-first.

    When ``task_id`` is provided, only jobs linked to that task are returned
    (skips the active/terminal window filter — returns all jobs for that task).
    """
    if task_id is not None:
        result = await session.execute(
            select(ComputeJob)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.task_id == task_id,
            )
            .order_by(ComputeJob.created_at.desc(), ComputeJob.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
        jobs = list(result.scalars().all())
        has_more = len(jobs) > limit
        return jobs[:limit], has_more
    if all_terminal:
        terminal_filter = ComputeJob.completed_at.is_not(None)
    else:
        cutoff = _utcnow() - include_finished_within
        terminal_filter = ComputeJob.completed_at.is_not(None) & (
            ComputeJob.completed_at >= cutoff
        )
    where_clauses = [
        ComputeJob.project_id == project_id,
        (
            ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES)
            | terminal_filter
        ),
    ]
    result = await session.execute(
        select(ComputeJob)
        .where(*where_clauses)
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


async def referenced_compute_image_digests(
    session, *, since: datetime | None = None
) -> set[str]:
    """``sha256:...`` digests of compute images a live/recent job still depends on.

    A baked compute-image digest must NOT be garbage-collected out of the
    registry while something still references it. A digest is "referenced" when
    it is the ``container_image`` of either:

      * a non-terminal compute job (``_COMPUTE_JOB_ACTIVE_STATUSES``) — a running
        or queued experiment that will still need to pull the image, OR
      * a terminal compute job whose ``completed_at >= since`` — a recent result
        whose provenance/reproducibility depends on the image staying pullable.
        When ``since`` is ``None`` only live jobs are considered.

    The image reference is stored in ``polling_metadata['container_image']`` as a
    digest-pinned ``registry/repo@sha256:<hex>`` (contributes its digest) or a
    tag-only ``registry/repo:tag`` (no stable digest — ignored). Returns the
    deduplicated set of ``sha256:...`` digests; an absent/tag-only/malformed
    reference contributes nothing.
    """
    conditions = [ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES)]
    if since is not None:
        conditions.append(
            and_(
                ComputeJob.status.in_(list(_COMPUTE_JOB_TERMINAL_STATUSES)),
                ComputeJob.completed_at.is_not(None),
                ComputeJob.completed_at >= since,
            )
        )
    result = await session.execute(
        select(ComputeJob.polling_metadata).where(or_(*conditions))
    )
    digests: set[str] = set()
    for meta in result.scalars().all():
        if not isinstance(meta, dict):
            continue
        ref = meta.get("container_image")
        if not isinstance(ref, str):
            continue
        digest = _parse_digest_from_pinned_ref(ref)
        if digest:
            digests.add(digest)
    return digests


_COMPUTE_JOB_BATCH_SIZE = 50
_COMPUTE_JOB_POLL_CONCURRENCY = 10


async def process_compute_jobs_once(session_factory=AsyncSessionLocal) -> None:
    last_created_at = None
    last_id = None
    _sem = asyncio.Semaphore(_COMPUTE_JOB_POLL_CONCURRENCY)

    while True:
        async with session_factory() as session:
            stmt = (
                select(ComputeJob)
                .where(ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES))
                .order_by(ComputeJob.created_at, ComputeJob.id)
                .limit(_COMPUTE_JOB_BATCH_SIZE)
            )
            if last_created_at is not None:
                stmt = stmt.where(
                    or_(
                        ComputeJob.created_at > last_created_at,
                        and_(
                            ComputeJob.created_at == last_created_at,
                            ComputeJob.id > last_id,
                        ),
                    )
                )
            result = await session.execute(stmt)
            jobs = result.scalars().all()

        # NOTE: the job rows above are a SNAPSHOT taken at tick start. Processing
        # a full batch takes as long as up-to-50 backend RPCs, so by the time a
        # late job in the batch is reached its row (and its task's row) may be
        # minutes old. Nothing here may act on that snapshot: the authoritative
        # rows are re-read per job in _process_single_compute_job, which is where
        # the (irreversible) task-terminal VM kill is decided. Deliberately NOT
        # batch-prefetching the tasks: a prefetched task snapshot would be exactly
        # the stale input the kill must never be decided on.

        if not jobs:
            break

        async def _run_job(job):
            async with _sem:
                try:
                    await _process_single_compute_job(job, session_factory=session_factory)
                except Exception as exc:
                    logger.exception(
                        "[COMPUTE_JOB] monitor failed for job=%s: %s",
                        job.id,
                        exc,
                    )

        await asyncio.gather(*[_run_job(job) for job in jobs])

        if len(jobs) < _COMPUTE_JOB_BATCH_SIZE:
            break

        last_created_at = jobs[-1].created_at
        last_id = jobs[-1].id


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
        _record_terminal_reason_history(session, live)
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


async def _maybe_retry_provisioning_timeout(
    session_factory,
    job_id: uuid.UUID,
) -> bool:
    """Check if a provisioning-timeout job should be retried with backoff.

    A job that TIMED_OUT during provisioning (deadline expired or launch LRO
    timed out) can be retried up to ``MAX_PROVISIONING_RETRY_COUNT`` times
    before going terminal, so transient GCP infrastructure issues (zone quota
    exhaustion, image family blips, transient 503s) are recovered without
    operator intervention.

    Returns True when the job's provisioning deadline has been extended for
    retry (the caller should return without marking the job terminal). Returns
    False when retry budget is exhausted (caller should proceed with the
    terminal transition).

    The retry count is stored in ``polling_metadata['provisioning_retry_count']``;
    the provisioning deadline is reset to ``<time of THIS retry> + backoff`` so
    the next monitor tick attempts a fresh launch.

    The retry clock starts NOW, never at the caller's tick-start ``now``. The
    caller reaches this function only AFTER a slow leg it just waited out — a
    launch LRO that ran to the full provisioning timeout (minutes), or a
    ``backend.terminate`` RPC. Deriving the new deadline from the tick-start
    timestamp made every retry deadline that much shorter than the backoff
    (routinely already in the PAST when it was written): retry 1 was dead on
    arrival, and the very next tick re-timed-it-out without ever attempting a
    fresh launch, burning the whole retry budget in seconds.
    """
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            return True  # Already terminal — concurrent race, no-op.
        meta = dict(live.polling_metadata or {})
        retry_count = int(meta.get("provisioning_retry_count", 0))
        if retry_count >= MAX_PROVISIONING_RETRY_COUNT:
            return False  # Budget exhausted — let the caller go terminal.
        retry_count += 1
        backoff = exponential_backoff(
            retry_count,
            base=PROVISIONING_RETRY_BACKOFF_BASE_SECONDS,
            cap=600.0,
        )
        # Real time AT RETRY — not the (already stale) tick-start timestamp.
        retry_at = _utcnow()
        new_deadline = retry_at + timedelta(seconds=backoff)
        meta["provisioning_retry_count"] = retry_count
        meta["provisioning_deadline_at"] = new_deadline.isoformat()
        meta["status_message"] = (
            f"provisioning timed out (retry {retry_count}/{MAX_PROVISIONING_RETRY_COUNT})"
        )
        live.polling_metadata = meta
        live.updated_at = retry_at
        session.add(live)
        await session.commit()
    logger.info(
        "[COMPUTE_JOB] provisioning retry %d/%d for job=%s (backoff=%.0fs)",
        retry_count,
        MAX_PROVISIONING_RETRY_COUNT,
        job_id,
        backoff,
    )
    return True


async def _process_single_compute_job(
    job: ComputeJob, *, session_factory=AsyncSessionLocal
) -> None:
    now = _utcnow()
    backend = get_compute_backend(job.backend)

    task = None
    if job.task_id is not None:
        # AUTHORITATIVE RE-READ before the (irreversible) task-terminal VM kill.
        #
        # ``job`` is a row from the monitor's batch snapshot: the tick selects up
        # to _COMPUTE_JOB_BATCH_SIZE jobs, then processes them one by one, each
        # doing backend RPCs. A job late in the batch is therefore decided on data
        # that can be many minutes old. Two ways that goes wrong, both fixed by
        # reading the rows again HERE, at the moment of decision:
        #   * the task was terminal at snapshot time but has since been restored
        #     (an operator PATCHes a falsely-FAILED task back to QUEUED) — the
        #     stale snapshot would KILL the VM of a task that is alive again;
        #   * the task went terminal DURING the tick — the stale snapshot would
        #     MISS the kill and keep billing the VM until the next tick.
        # The job row is re-read in the same round trip: a peer (operator kill,
        # another leader) may already have driven it terminal, in which case there
        # is nothing left for this tick to do.
        async with session_factory() as session:
            live = await session.get(ComputeJob, job.id)
            if live is None or live.status in _COMPUTE_JOB_TERMINAL_STATUSES:
                return
            job = live
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
            if await _maybe_retry_provisioning_timeout(session_factory, job.id):
                return
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
                session_factory, reserved, _build_launch_spec(reserved), task=task
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
            if await _maybe_retry_provisioning_timeout(session_factory, job.id):
                return
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
        except ComputeLaunchSpecError as exc:
            # A deterministic spec defect (e.g. an env key that is not a shell
            # identifier). The generic handler below is retry-shaped: it records a
            # diagnostic and re-polls, so this same exception is re-raised every
            # monitor tick until provisioning_deadline_at, and the job finally
            # finalizes TIMED_OUT with "compute job timed out while provisioning" —
            # a terminal reason that never names the offending field. Retrying an
            # unretryable spec buys nothing, so terminalize now and put the
            # exception message (which names the bad key) in terminal_reason, where
            # the operator actually reads it.
            #
            # On today's code the raise happens before provision_ephemeral_vm (the
            # startup script is rendered as an ARGUMENT to it), so no billable
            # resource exists and cost is zero. The terminate call below is NOT
            # conditional on that holding: _reserve_compute_job_launch always
            # assigns external_compute_id before launch, so `terminated` is always
            # a real backend.terminate() — a belt-and-braces reap that costs one
            # 404-ing lookup on this rare path and is the only thing that keeps a
            # future post-provision raise from LEAKING a billed GPU VM. Deliberately
            # kept unconditional for that reason; see
            # test_process_compute_jobs_once_reaps_vm_when_launch_rejects_post_provision.
            failed_at = _utcnow()
            logger.warning(
                "[COMPUTE_JOB] launch rejected for job=%s (unretryable launch spec): %s",
                job.id,
                exc,
            )
            terminated = True
            if reserved.external_compute_id:
                try:
                    terminated = await backend.terminate(
                        reserved, reason="launch_spec_rejected"
                    )
                except Exception:
                    logger.exception(
                        "[COMPUTE_JOB] terminate failed for rejected-spec job=%s",
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
                terminal_status=ComputeJobStatus.FAILED,
                outcome=ComputeJobOutcome.FAILURE,
                terminal_reason=str(exc),
                terminate_ok=terminated,
                compute_rpc_diagnostic=_build_compute_rpc_diagnostic(
                    job,
                    operation="launch",
                    exc=exc,
                    started_at=started_at,
                    failed_at=failed_at,
                    next_retry_at=None,
                    reason="launch_spec_rejected",
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
        no_progress_like = _status_is_no_progress_timeout(status)
        # A backend-reported SUCCEEDED that nonetheless emitted a fatal
        # no-usable-GPU diagnostic is not a real success — the container never
        # had a supported GPU and could not produce its metric. Reclassify it as
        # a FAILURE so the GPU/image mismatch surfaces loudly.
        no_usable_gpu = (
            not timeout_like
            and status.status == ComputeJobStatus.SUCCEEDED
            and _status_reports_no_usable_gpu(status)
        )
        # A backend-reported SUCCEEDED for a job that DECLARED a scalar metric or
        # per-arm output but never fulfilled that contract is not a real success.
        # Incidental durable files are preserved by _link_terminal_artifact, but
        # cannot prove delivery: a log or checkpoint does not measure the declared
        # result. Guarded by _job_expects_data so pure side-effect commands (no
        # metric, no arms) are NOT false-rejected. Artifact detection remains
        # best-effort and never raises (collect_terminal_artifact_reference is
        # fail-soft by contract).
        #
        # For a job with a contract-declared SCALAR metric, defer to the precise
        # _declared_metric_unmeasured resolver, which mirrors the success-criterion
        # gate's FULL resolution (result file, per-arm block, stdout fallback, and
        # the sticky metric a PRIOR poll persisted). That prevents the coarse
        # current-poll metrics check from false-rejecting a run whose metric was
        # only ever reported via stdout, inside an arm, or on an earlier poll. An
        # arms-only sweep instead requires at least one numeric per-arm result;
        # a flat metric cannot fulfill that distinct contract.
        #
        # Separate path: compute_experiment tasks that declare NO metric and NO
        # success_criterion are criterion-free experiments.  _job_expects_data does
        # not fire for them (no declared_metric, no arms), but they still must not
        # record a clean SUCCESS — a metric is definitionally mandatory for experiments.
        declared_metric = _declared_progress_metric(job)
        _criterion_free_experiment = _is_criterion_free_experiment(job, task)
        no_data = (
            not timeout_like
            and not no_usable_gpu
            and status.status == ComputeJobStatus.SUCCEEDED
            and (
                _criterion_free_experiment
                or (
                    _job_expects_data(job, task)
                    and (
                        (declared_metric is not None and _declared_metric_unmeasured(job, status))
                        or (_task_declares_arms(task) and not _arms_have_reported_data(job, status))
                    )
                )
            )
        )
        terminal_artifact_reference = (
            await collect_terminal_artifact_reference(job) if no_data else None
        )
        if timeout_like or no_progress_like:
            outcome = ComputeJobOutcome.TIMED_OUT
        elif no_usable_gpu or no_data:
            outcome = ComputeJobOutcome.FAILURE
        elif status.status == ComputeJobStatus.SUCCEEDED:
            outcome = ComputeJobOutcome.SUCCESS
        else:
            outcome = ComputeJobOutcome.FAILURE
        await _apply_polled_compute_job_result(
            session_factory,
            job.id,
            now=finished,
            poll_attempt_count=poll_attempt_count,
            next_poll_at=next_poll_at,
            status=status,
            accrued_cost_usd=accrued_cost_usd,
            terminal_status=ComputeJobStatus.TIMED_OUT
            if timeout_like or no_progress_like
            else status.status,
            outcome=outcome,
            terminal_reason=(
                "compute job exceeded runtime deadline"
                if timeout_like
                else (
                    "no_progress_timeout"
                    if no_progress_like
                    else (
                    _NO_USABLE_GPU_TERMINAL_REASON
                    if no_usable_gpu
                    else (
                        _NO_CRITERION_EXPERIMENT_REASON
                        if _criterion_free_experiment
                        else (
                            _no_data_terminal_reason(terminal_artifact_reference)
                            if no_data
                            else _resolve_terminal_reason(
                                status_message=status.message, outcome=outcome
                            )
                        )
                    )
                    )
                )
            ),
            exit_code=status.exit_code,
            terminal_kind=NO_PROGRESS_TERMINAL_REASON if no_progress_like else None,
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

    # No-progress watchdog: cut a still-running job whose declared metric has
    # plateaued so a multi-day run cannot burn its whole deadline learning
    # nothing. Runs AFTER the deadline/budget hard limits so those always win.
    if _no_progress_stalled(job, now=now):
        watched_metric = _declared_progress_metric(job)
        # Progressive operator notification BEFORE the cut — fires a WARNING
        # alert so the operator learns about the stall from the Alerts banner,
        # not only after the job reaches TIMED_OUT. Best-effort: a failure to
        # create the alert (e.g. DB blip) must not prevent the terminate from
        # proceeding — the job is still stalled and should be cut.
        try:
            async with session_factory() as _alert_session:
                await _maybe_create_no_progress_watchdog_alert(
                    _alert_session, job, now=now, watched_metric=watched_metric
                )
        except Exception:
            logger.exception(
                "[COMPUTE_JOB] no-progress alert creation failed for job=%s (proceeding to terminate)",
                job.id,
            )
        started_at = _utcnow()
        terminate_diagnostic = None
        try:
            terminated = await backend.terminate(job, reason="no_progress")
        except Exception as exc:
            failed_at = _utcnow()
            logger.exception(
                "[COMPUTE_JOB] no-progress terminate failed for job=%s: %s",
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
                reason="no_progress",
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
            terminal_reason=(
                "no_progress_timeout: compute job made no measurable progress on declared metric "
                f"{watched_metric!r} over "
                f"{int((job.polling_metadata or {}).get('no_progress_timeout_seconds') or COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS)}s — "
                "cut early to reclaim compute"
            ),
            terminal_kind=NO_PROGRESS_TERMINAL_REASON,
            terminate_ok=terminated,
            compute_rpc_diagnostic=terminate_diagnostic,
        )
        return

    # Honesty check: the watchdog above keys on EXACT metric-name equality, so a
    # job that reports progress only under name(s) other than its declared metric
    # leaves the watchdog silently inert — it can never fire and the operator's
    # waste-prevention lever protects nothing. Surface that mismatch (durable
    # marker + one warning log) instead of failing silent. Never itself kills a
    # job; it only makes the inert-watchdog condition observable.
    metric_mismatch = _watchdog_metric_name_mismatch(job)
    if metric_mismatch:
        logger.warning(
            "[COMPUTE_JOB] no-progress watchdog INERT for job=%s: declared metric "
            "%r is never reported; progress arrives only under %s — the watchdog "
            "cannot fire, so this run has no no-progress protection",
            job.id,
            _declared_progress_metric(job),
            metric_mismatch,
        )

    await _apply_polled_compute_job_result(
        session_factory,
        job.id,
        now=now,
        poll_attempt_count=poll_attempt_count,
        next_poll_at=next_poll_at,
        status=status,
        accrued_cost_usd=accrued_cost_usd,
        progress_metric_mismatch=metric_mismatch,
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
    Only metrics, command_log, echoed_release_sha, and resume_provenance are
    overridden by non-None values from the post-terminate poll so late-emitted
    evidence is not dropped.
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
        resume_provenance=salvaged.resume_provenance
        if salvaged.resume_provenance is not None
        else base.resume_provenance,
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
    terminal_kind: str | None = None,
    compute_rpc_diagnostic: dict[str, Any] | None = None,
    progress_metric_mismatch: list[str] | None = None,
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
        # Propagate resume provenance from the serial/launch status into
        # polling_metadata so it reaches the operator and agent surfaces via
        # serialize_compute_job_response. Only written once (first poll that
        # carries it); subsequent polls with the same provenance are idempotent.
        if status.resume_provenance is not None:
            _set_meta(live, resume_provenance=dict(status.resume_provenance))
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
                terminal_kind=terminal_kind,
            )
            _record_terminal_reason_history(session, live)
            if compute_rpc_diagnostic is not None:
                _set_meta(live, compute_rpc_diagnostic=compute_rpc_diagnostic)
            await _link_terminal_artifact(session, live)
            await persist_viewable_files_for_job(session, live)
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
            _apply_watchdog_metric_mismatch_marker(live, progress_metric_mismatch)
        await session.commit()
        return True


async def finalize_runner_subprocess_job_for_dead_runner(
    session_factory,
    job_id: uuid.UUID,
    *,
    runner_id: uuid.UUID,
    liveness_cutoff: datetime,
    now: datetime,
) -> bool:
    """Fail a claimed runner job when its runner is still confirmed dead.

    The reaper's candidate scan is deliberately only an optimization.  Re-check
    the runner under the job lock here, immediately before the irreversible
    terminal transition, so a recovered runner or a normal terminal report wins
    any race with the sweep.
    """
    async with session_factory() as session:
        live = await _lock_compute_job(session, job_id)
        if (
            live is None
            or live.backend != RUNNER_SUBPROCESS_BACKEND_NAME
            or live.status not in {ComputeJobStatus.STARTING, ComputeJobStatus.RUNNING}
            or live.ran_on_runner_id != runner_id
        ):
            return False

        runner = await session.get(LocalRunner, runner_id, with_for_update=True)
        if runner is None or (
            runner.status in ("active", "paused", "draining")
            and runner.last_seen_at is not None
            and runner.last_seen_at >= liveness_cutoff
        ):
            # A paused or draining runner that is still heartbeating IS alive
            # and will report its in-flight compute job result normally once
            # the job completes. Only fail the job when the runner has truly
            # stopped heartbeating (genuinely dead/hung).
            return False

        _finalize_terminal_job(
            live,
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.FAILURE,
            terminal_reason="runner_subprocess_runner_dead",
            now=now,
        )
        _record_terminal_reason_history(session, live)
        await _link_terminal_artifact(session, live)
        await persist_viewable_files_for_job(session, live)
        if live.task_id is not None:
            from app.task_lifecycle import fire_task_wakes

            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(live.id),
            )
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
        # Deliberately a FRESH read under the job lock — the caller's task
        # snapshot can be minutes stale by the end of a batch, and a task
        # revived in that window (e.g. FAILED→QUEUED restore) must not get
        # its running job cancelled on stale terminal status.
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
        _record_terminal_reason_history(session, live)
        if compute_rpc_diagnostic is not None:
            _set_meta(live, compute_rpc_diagnostic=compute_rpc_diagnostic)
        else:
            _clear_compute_rpc_diagnostic(live)
        await _link_terminal_artifact(session, live)
        await persist_viewable_files_for_job(session, live)
        if live.task_id is not None:
            from app.task_lifecycle import fire_task_wakes

            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(live.id),
            )
        await session.commit()
        return True


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    """Best-effort parse of a persisted UUID string; None on any malformed value."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


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
        no_progress_timeout_seconds=(
            int(meta.get("no_progress_timeout_seconds"))
            if meta.get("no_progress_timeout_seconds") is not None
            else None
        ),
        input_artifact_job_id=meta.get("input_artifact_job_id") or None,
    )


async def _attach_repo_clone(
    session_factory, job: ComputeJob, spec: ComputeLaunchSpec, *, task: Task | None = None
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
                _task = task
                if _task is None:
                    _task = await session.get(Task, job.task_id)
                if _task is not None:
                    payload = (
                        _task.first_payload
                        if isinstance(_task.first_payload, dict)
                        else {}
                    )
                    ctx = _task.context if isinstance(_task.context, dict) else {}
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


# polling_metadata key under which the poller records that the no-progress
# watchdog is inert because the job reports progress under name(s) other than its
# declared metric. Surfaced to the operator via serialize_compute_job_response so
# the silent-degradation edge is observable and fixable, not invisible.
_WATCHDOG_STATUS_META_KEY = "no_progress_watchdog_status"


def _apply_watchdog_metric_mismatch_marker(
    job: ComputeJob, observed_metrics: list[str] | None
) -> None:
    """Stamp (or clear) the metric-name-mismatch watchdog marker on a running job.

    ``observed_metrics`` is the non-empty list returned by
    :func:`_watchdog_metric_name_mismatch` when the declared metric is never
    reported; a falsy value clears any previously-stamped marker (the job started
    reporting its declared metric, so the watchdog is live again).
    """
    meta = dict(job.polling_metadata or {})
    if observed_metrics:
        meta[_WATCHDOG_STATUS_META_KEY] = {
            "state": "inert_metric_name_mismatch",
            "declared_metric": _declared_progress_metric(job),
            "observed_metrics": observed_metrics,
        }
        job.polling_metadata = meta
    elif _WATCHDOG_STATUS_META_KEY in meta:
        meta.pop(_WATCHDOG_STATUS_META_KEY, None)
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
    terminal_kind: str | None = None,
) -> None:
    job.status = status
    job.outcome = outcome
    job.completed_at = now
    job.last_polled_at = job.last_polled_at or now
    job.terminal_reason = terminal_reason
    job.final_cost_usd = job.accrued_cost_usd
    job.updated_at = now
    resolved_kind = terminal_kind or _OUTCOME_TO_TERMINAL_KIND.get(outcome, "failure")
    extra: dict[str, Any] = {"terminal_status": status.value, "terminal_kind": resolved_kind}
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


def _record_terminal_reason_history(session, job: ComputeJob) -> None:
    """Append a durable TaskHistory row carrying WHY the compute job ended.

    ``terminal_reason`` otherwise lives only in mutable channels
    (``latest_status_message`` / ``polling_metadata.status_message``) that the
    next reap or status update overwrites. Every non-success terminal (deadline
    kill, spend cap, provisioning timeout, runner-reported failure,
    cancellation) gets a history row so operators and follow-on agents can
    always recover the reason, not just the FAILED state. SUCCESS is skipped —
    this is the failure-reason channel, not a completion log.

    Add-only (``session.add``); the caller owns the commit alongside the
    terminal transition itself.
    """
    if job.task_id is None:
        return
    outcome = getattr(job.outcome, "value", job.outcome)
    if outcome == ComputeJobOutcome.SUCCESS.value:
        return
    details: dict[str, Any] = {
        "compute_job_id": str(job.id),
        "status": getattr(job.status, "value", job.status),
        "outcome": outcome,
        "terminal_reason": job.terminal_reason,
    }
    exit_code = (job.polling_metadata or {}).get("exit_code")
    if exit_code is not None:
        details["exit_code"] = exit_code
    session.add(
        TaskHistory(
            task_id=job.task_id,
            action="COMPUTE_TERMINATED",
            details=details,
        )
    )


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
            elif (
                name.endswith("__series")
                and isinstance(value, list)
                and not isinstance(value, (str, bytes))
            ):
                # Ordered per-emission series for a metric a single-command A/B
                # experiment printed once per arm (see _stdout_metrics_fallback).
                # The numeric loop drops non-scalar values, so preserve the list
                # explicitly — like the arms block below — keeping the full
                # comparison structurally recoverable rather than reducing the
                # proof to its final arm.
                coerced = [
                    num
                    for v in value
                    for num in (_coerce_metric_number(v),)
                    if num is not None
                ]
                if len(coerced) >= 2:
                    result[name] = coerced
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


async def build_compute_result_logs_async(job: ComputeJob) -> str:
    """Async wrapper around :func:`build_compute_result_logs`.

    Offloads ``json.dumps`` to a thread-pool worker so the event loop
    is not blocked when serializing large result payloads.  Production
    callers that already run in an async context should use this;
    synchronous callers (notably tests) can call
    :func:`build_compute_result_logs` directly.
    """
    return await asyncio.to_thread(build_compute_result_logs, job)


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
    # Best-effort: persist common viewable files (images/videos) as individually-
    # addressable objects + a recorded manifest so an operator can preview them
    # inline without untarring the bundle. Runs the SINGLE viewable pipeline
    # (download once, sanitize once, upload once, one cap policy) and records it on
    # both persisted surfaces — the manifest and the ComputeJobFile rows. Idempotent:
    # when the caller already ran the pass for this job, the stamped manifest comes
    # back with no second download/upload. Fail-soft — any error degrades to no
    # manifest and NEVER blocks the job's terminal completion (mirrors the
    # surrounding helpers). Stamped onto polling_metadata regardless of whether the
    # Artifact INSERT below succeeds, so the manifest survives a linkage rollback.
    viewable_files, viewable_files_dropped = await persist_viewable_files_for_job(
        session, job
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
            if viewable_files:
                artifact_logs["viewable_files"] = viewable_files
            if viewable_files_dropped:
                artifact_logs["viewable_files_dropped"] = viewable_files_dropped
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


# ---------------------------------------------------------------------------
# Viewable-file persistence from durable artifact bundles — ONE pipeline
# ---------------------------------------------------------------------------
#
# One download, one sanitizer, one cap policy, one upload.
#
# There used to be TWO overlapping viewable-artifact pipelines, BOTH running at
# every terminal transition of a gcp/runner-subprocess job:
#
#   * ``persist_viewable_artifacts`` (app.compute_backends) — STREAMS the bundle to
#     a temp file (never into the orchestrator's heap; a results bundle has no size
#     bound), sanitizes member paths with ``storage.sanitize_viewable_relpath``
#     (subdir context folded to ``__``), caps at 8 MiB/file and 25 files (applied to
#     a deterministically SORTED candidate list, BEFORE any member bytes are read),
#     uploads to the ``artifacts/viewable/`` prefix, and returns the
#     ``viewable_files`` manifest.
#   * a second extractor that lived HERE — downloaded the SAME bundle AGAIN,
#     sanitized with a DIFFERENT sanitizer (kept ``/`` separators), capped at
#     25 MB/file and 50 files (first-50 in arbitrary TAR ORDER), and uploaded every
#     file a SECOND time to a DIFFERENT prefix (``artifacts/files/``).
#
# So the bundle was fetched twice and every viewable file was stored twice under
# divergent rules — and because the caps and the sanitizers disagreed, the two
# copies could disagree about WHICH files exist and under WHAT names: a 20 MB plot
# was present under one prefix and absent from the other, and ``arm1/reward.png``
# was ``arm1__reward.png`` in one and ``arm1/reward.png`` in the other.
#
# The ``artifacts/viewable/`` manifest is the CANONICAL surface. It is what the
# admin UI renders (``viewable_artifacts``, normalized in
# ``routers/admin_compute_jobs.py``), what the strategic context package reads, and
# what the artifact download-URL endpoint signs. The ``artifacts/files/`` objects
# were never handed to any client — nothing ever listed them — so they were
# undiscoverable duplicates of the canonical ones.
#
# The canonical pipeline now runs ONCE and its single result is recorded on BOTH
# persisted surfaces: the ``viewable_files`` manifest AND the ``ComputeJobFile``
# rows (the DB-side signing allowlist read by the download-URL endpoint). The rows
# now carry the SAME canonical ``viewable/`` URIs as the manifest, so the two
# surfaces can no longer diverge.


async def _record_viewable_job_files(
    session, job: ComputeJob, manifest: list[dict[str, Any]]
) -> int:
    """Mirror a viewable manifest into ``ComputeJobFile`` rows (signing allowlist).

    One row per durably-stored file, pointing at the SAME canonical ``viewable/``
    URI the manifest already carries — no second upload, no second sanitizer, no
    second cap policy. Existing deterministic-URI rows are updated in place; each
    new INSERT runs in its own SAVEPOINT so one constraint violation or DB hiccup
    cannot lose rows already written or roll back terminal completion.

    Best-effort by contract: returns the number of rows written, never raises.
    """
    if not manifest:
        return 0
    written = 0
    # Pre-filter to valid entries so we can batch-fetch existing rows in one
    # query instead of N individual SELECTs (N ≤ 25 due to per-job cap).
    valid_entries = [
        e for e in manifest
        if isinstance(e, dict)
        and isinstance(e.get("storage_uri"), str) and e.get("storage_uri")
        and isinstance(e.get("filename"), str) and e.get("filename")
    ]
    if not valid_entries:
        return 0
    all_uris = [e["storage_uri"] for e in valid_entries]
    existing_rows_result = await session.execute(
        select(ComputeJobFile).where(ComputeJobFile.storage_uri.in_(all_uris))
    )
    existing_by_uri: dict[str, ComputeJobFile] = {
        row.storage_uri: row for row in existing_rows_result.scalars().all()
    }
    for entry in valid_entries:
        storage_uri = entry.get("storage_uri")
        filename = entry.get("filename")
        content_type = entry.get("content_type")
        size_bytes = entry.get("size_bytes")
        # ``kind`` comes straight from the shared extension classifier
        # ("image"/"video"); fall back to the MIME mapping only if it is absent.
        media_type = entry.get("kind")
        if not isinstance(media_type, str) or not media_type:
            media_type = (
                storage._media_kind_for_mime(content_type)
                if isinstance(content_type, str)
                else "unknown"
            )
        try:
            existing_row = existing_by_uri.get(storage_uri)
            if existing_row is not None:
                # Mid-run registration already created the signing row.
                # Reconcile its mutable metadata to the authoritative terminal
                # bundle without adding a duplicate deterministic-URI row.
                existing_row.safe_relpath = filename
                existing_row.media_type = media_type
                existing_row.content_type = (
                    content_type if isinstance(content_type, str) else None
                )
                existing_row.file_size_bytes = (
                    size_bytes if isinstance(size_bytes, int) else None
                )
                session.add(existing_row)
                continue
            async with session.begin_nested():
                row = ComputeJobFile(
                    compute_job_id=job.id,
                    safe_relpath=filename,
                    media_type=media_type,
                    storage_uri=storage_uri,
                    content_type=(
                        content_type if isinstance(content_type, str) else None
                    ),
                    file_size_bytes=(
                        size_bytes if isinstance(size_bytes, int) else None
                    ),
                )
                session.add(row)
                await session.flush()
            written += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "[COMPUTE_JOB] ComputeJobFile insert failed for viewable file %r "
                "in job=%s",
                filename,
                job.id,
                exc_info=True,
            )
    return written


async def _purge_orphaned_viewable_rows(
    session, job: ComputeJob, manifest: list[dict[str, Any]]
) -> int:
    """Delete ComputeJobFile rows not in the terminal manifest, plus their GCS objects.

    Called only when the terminal extraction is complete (authoritative). Mid-run
    uploads that were not selected into the top-25 manifest leave orphaned rows and
    GCS objects; this purge reconciles them to the authoritative manifest surface.
    Best-effort: never raises.
    """
    keep_uris = {
        entry["storage_uri"]
        for entry in manifest
        if isinstance(entry, dict) and isinstance(entry.get("storage_uri"), str)
    }
    try:
        all_rows = (
            await session.execute(
                select(ComputeJobFile).where(ComputeJobFile.compute_job_id == job.id)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPUTE_JOB] failed to query ComputeJobFile rows for orphan purge job=%s",
            job.id,
            exc_info=True,
        )
        return 0

    purged = 0
    for row in all_rows:
        if row.storage_uri in keep_uris:
            continue
        try:
            await storage.delete_storage_uri_best_effort(row.storage_uri)
            await session.delete(row)
            purged += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "[COMPUTE_JOB] failed to purge orphaned ComputeJobFile row %s job=%s",
                row.storage_uri,
                job.id,
                exc_info=True,
            )
    if purged:
        logger.info(
            "[COMPUTE_JOB] purged %d orphaned ComputeJobFile row(s) for job=%s",
            purged,
            job.id,
        )
    return purged


async def persist_viewable_files_for_job(
    session, job: ComputeJob
) -> tuple[list[dict[str, Any]], int]:
    """Persist a terminal job's viewable files — the SINGLE viewable pipeline.

    Delegates selection and upload to :func:`persist_viewable_artifacts`, the
    canonical backend-aware pass (ONE bundle download for gcp/runner-subprocess —
    STREAMED to a temp file, never held in the orchestrator's heap — an on-disk walk
    for local backends; ``storage.sanitize_viewable_relpath``; 8 MiB per-file and
    25-file caps over a deterministically sorted candidate list), then records that
    ONE result on both durable surfaces:

    * ``polling_metadata['viewable_files']`` (plus ``viewable_files_dropped`` and
      ``viewable_file_count``) — read by the admin UI (``viewable_artifacts``) and
      the strategic context package, and used as the signing allowlist by the
      artifact download-URL endpoint;
    * ``ComputeJobFile`` rows — the DB-side signing allowlist for that same
      endpoint, carrying the SAME canonical URIs.

    IDEMPOTENT. ``viewable_file_count`` is stamped on every completed pass (even a
    zero-file one), so a re-entry — the same terminal transition reaching both this
    call site and :func:`_link_terminal_artifact` — returns the stamped manifest
    WITHOUT re-downloading the bundle or re-uploading a byte. That guard is what
    holds the bundle download to exactly one per job.

    Fail-soft by contract: any extraction/upload/DB error NEVER blocks terminal
    completion. An incomplete terminal pass preserves already-durable live
    entries and deliberately does not stamp the idempotency marker, so a later
    terminal reconciliation can retry. Returns ``(manifest, dropped)``.
    """
    meta = job.polling_metadata or {}
    if "viewable_file_count" in meta:
        # This job's viewable pass already ran — never re-download / re-upload.
        stamped = meta.get("viewable_files")
        dropped = meta.get("viewable_files_dropped")
        return (
            stamped if isinstance(stamped, list) else [],
            dropped if isinstance(dropped, int) else 0,
        )

    complete = False
    svg_count = 0
    try:
        manifest, dropped, complete, svg_count = (
            await persist_viewable_artifacts_with_completeness(job)
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPUTE_JOB] viewable-artifact persistence failed for job=%s; "
            "preserving durable live previews for a later retry",
            job.id,
            exc_info=True,
        )
        manifest, dropped = [], 0

    # Terminal bundle extraction is authoritative. Ensure every terminal entry
    # has an opaque revision even when a legacy/custom backend returned the old
    # shape, and clear the live marker before publishing the reconciled manifest.
    terminal_manifest: list[dict[str, Any]] = []
    for raw_entry in manifest if isinstance(manifest, list) else []:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry["revision"] = (
            storage.normalize_viewable_revision(entry.get("revision"))
            or storage.new_viewable_revision()
        )
        entry["live"] = False
        terminal_manifest.append(entry)
    if complete:
        manifest = terminal_manifest
    else:
        # A fail-soft extraction or upload pass is not authoritative. Reconcile
        # the terminal entries that did persist, but retain prior live entries
        # for filenames the incomplete pass could not reproduce. Their objects
        # and ComputeJobFile signing rows are already durable; hiding them would
        # lose useful output while also stamping the pass as non-retryable.
        terminal_names = {
            entry.get("filename")
            for entry in terminal_manifest
            if isinstance(entry.get("filename"), str)
        }
        prior_live = [
            dict(entry)
            for entry in (meta.get("viewable_files") or [])
            if isinstance(entry, dict)
            and entry.get("live") is True
            and entry.get("filename") not in terminal_names
        ]
        manifest = terminal_manifest + prior_live

    await _record_viewable_job_files(session, job, manifest)

    if complete:
        await _purge_orphaned_viewable_rows(session, job, manifest)

    reconciled_meta = dict(job.polling_metadata or {})
    reconciled_meta["viewable_files"] = manifest
    if complete:
        # A complete terminal enumeration is authoritative, including an empty
        # one. Stamp the idempotency marker only now; this also clears any prior
        # live omission count and incomplete-pass diagnostic.
        reconciled_meta["viewable_file_count"] = len(manifest)
        reconciled_meta["viewable_files_dropped"] = dropped
        if svg_count:
            reconciled_meta["viewable_files_svg_excluded"] = svg_count
        else:
            reconciled_meta.pop("viewable_files_svg_excluded", None)
        reconciled_meta.pop("viewable_terminal_reconciliation_incomplete", None)
    else:
        # Leave viewable_file_count absent so the next terminal call retries.
        reconciled_meta.pop("viewable_file_count", None)
        reconciled_meta["viewable_terminal_reconciliation_incomplete"] = True
    job.polling_metadata = reconciled_meta
    session.add(job)
    return manifest, dropped
