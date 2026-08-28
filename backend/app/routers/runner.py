"""Runner-facing polling endpoints for the pull-based local fleet runner.

A runner process on operator hardware authenticates with its bearer token (see
``app.auth.get_current_runner``) and:

  * POST /api/runner/next-task — pulls the next task the orchestrator pinned to
    this runner and receives a task-scoped agent JWT. The task stays in
    PROVISIONING; the agent binary calls ``/api/agent/claim`` which performs
    the PROVISIONING → IN_PROGRESS transition (same contract as GCP VMs).
  * POST /api/runner/heartbeat — reports liveness + currently-active task ids so
    the orchestrator's dispatch cache and stale-runner sweep can track it.
  * POST /api/runner/next-compute-job — pulls the next STARTING runner-subprocess
    compute job pinned to this runner's project, atomically transitioning it to
    RUNNING (claim). Returns the exec fields the runner needs to execute the arm.
  * POST /api/runner/compute-job/{job_id}/report — runner reports exec_status
    (running|succeeded|failed), exit_code, result_metrics, command_log, and an
    optional self-reported ``progress`` tail (validated + merged into
    polling_metadata["progress"]); interim running reports stamp their bounded
    command_log tail as polling_metadata["live_output"]. The monitor's existing
    poll_status/finalize path picks up the stamped terminal status. Returns
    {ok, cancel_requested}.

No LOCAL_MODE flag is involved: the orchestrator pins QUEUED tasks to an active
registered runner (vm_instance_id='runner:{id}', context['runner_target_id']),
and this endpoint hands those pinned tasks to the polling runner.
"""

import asyncio
import base64
import binascii
import logging
import math
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.auth import create_agent_token, get_current_runner
from app.field_encryption import decrypt_field
from app.broadcast import broadcast_to_project
from app import storage as app_storage
from app.runner_project_binding import allowed_project_ids
from app.runner_pins import release_unpulled_runner_pins
from app.compute_backends import (
    RUNNER_SUBPROCESS_BACKEND_NAME,
    _MAX_VIEWABLE_FILE_BYTES,
    _MAX_VIEWABLE_FILE_COUNT,
    _MAX_VIEWABLE_VIDEO_BYTES,
)
from app.compute_jobs import append_progress_sample, normalize_terminal_command_log
from app.runner_rate_limiter import check_runner_rate_limit
from app.runner_config import config_generation, live_safe_config
from app.runner_pull_skip import (
    SKIP_CODE_CAPABILITY_MISMATCH,
    SKIP_CODE_CREDENTIAL_ERROR,
    SKIP_CODE_NO_ACCOUNT,
    cached_skip,
    clear_skip_marker,
    has_skip_marker,
    maybe_escalate_pull_skip,
    release_capability_mismatched_task,
    skip_marker_is_fresh,
    skip_seen_at,
    stamp_skip_reason,
)
from app.alert_policy import create_project_alert_if_allowed
from app.database import get_session
from app.execution_principal import resolve_project_execution_principal
from app.models import Alert, AlertSeverity, ComputeJob, ComputeJobFile, ComputeJobStatus, LocalRunner, Project, RunnerHeartbeatSnapshot, Task, TaskStatus, _utcnow
from app.runner_decline import (
    DECLINE_ESCALATION_THRESHOLD as _DECLINE_ESCALATION_THRESHOLD,
    maybe_create_decline_alert as _maybe_create_decline_alert,
    record_runner_decline,
)
from app.task_capabilities import agent_capability_slice
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.runner_health_monitors import _RUNNER_LIVENESS_SECONDS
from app.workbench_state import (
    refresh_strategic_local_continuation_on_runner_heartbeat,
)
from app.time_utils import utc_iso

_MAX_RUNNER_COMMAND_LOG_BYTES = 65536
_MAX_RUNNER_RESULT_METRICS_KEYS = 256
_MAX_RUNNER_RESULT_STRING_BYTES = 4096
# v1 cap on durable artifact uploads (mirrors _RUNNER_ARTIFACT_MAX_BYTES on the
# runner). The runner already gates on the uncompressed total; this caps the
# actual uploaded tarball as a server-side defense.
_MAX_RUNNER_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB
# Allowance for multipart framing/headers when early-rejecting on Content-Length.
_MULTIPART_BODY_MARGIN = 1 * 1024 * 1024  # 1 MiB
# Non-file scalar field size cap in multipart forms.  Filenames and other
# metadata are a few hundred bytes at most; 4 KiB is far beyond any legitimate
# value and bounds the heap impact of a crafted oversized field.
_MAX_MULTIPART_FIELD_BYTES = 4 * 1024  # 4 KiB
# Hard upper bound on boundary markers in a multipart body.  A normal
# viewable upload has 3 parts (filename scalar + file part + trailing
# close-boundary); capping at 64 prevents a crafted body from growing the
# offset list without bound.
_MAX_MULTIPART_PARTS = 64
_ARTIFACT_TRANSFER_TTL_SECONDS = 60 * 60
# Interim live-output tail (running reports only). Stamped as
# polling_metadata["live_output"] — the same key/shape the GCP live-tail path
# writes (compute_jobs._apply_status_update + agent_compute_jobs heartbeat) and
# the admin UI's live-output block reads. The runner sends ~4 KiB; server cap.
_MAX_RUNNER_LIVE_OUTPUT_BYTES = 8192
# Terminal-cause string a runner may attach to a terminal report
# (ComputeJobReportRequest.terminal_reason). The real emitters are single
# sentences ("runner provisioning failure: no usable python runtime on this
# host"), so 500 chars is far beyond any legitimate value while bounding what
# a crafted report can write into the JSONB polling_metadata column.
_MAX_RUNNER_TERMINAL_REASON_CHARS = 500
# Self-reported progress records (BLUEPRINT_PROGRESS_FILE protocol). Written by
# sandboxed experiment code and relayed by the runner, so the server validates
# every field and bounds every dimension before storing.
_MAX_PROGRESS_RECORDS_STORED = 50
_MAX_PROGRESS_RECORDS_PER_REPORT = 20
_MAX_PROGRESS_METRIC_CHARS = 128
_MAX_PROGRESS_TS_CHARS = 64
_MAX_PROGRESS_COUNT = 1_000_000_000
# Heartbeat capability-list bounds (defense against a garbage-emitting runner).
_MAX_GPU_LIST_ENTRIES = 64
# Upper clamp for per-GPU VRAM heartbeat fields. 4 TiB per entry is far beyond
# any real device, so an absurd runner-reported integer can never reach the
# authoring context / agent prompts while every plausible value passes through.
_MAX_GPU_MEMORY_MIB = 4_194_304  # 4 TiB
_MAX_CLI_INVENTORY_ENTRIES = 16
_MAX_CLI_VERSION_CHARS = 128
# Bound the runner-reported per-project image-cache map: a runner's allow-list is
# small, so an unbounded project map would be a malformed/hostile report.
_MAX_IMAGE_CACHE_PROJECTS = 64
# 1 PiB per bucket — far beyond any real image cache, so an absurd runner-reported
# integer can never poison the telemetry while every plausible value passes.
_MAX_IMAGE_CACHE_BYTES = 1 << 50
_MAX_RESOURCE_CAPACITY_BYTES = 1 << 60
_MAX_RESOURCE_CAPACITY_CORES = 1_000_000.0
_MAX_RESOURCE_CAPACITY_COUNT = 1_000_000


def _observed_runner_hardware(runner: LocalRunner) -> dict[str, Any]:
    """Return a bounded snapshot of the hardware that actually ran a job."""
    caps = dict(runner.capabilities or {})
    hardware: dict[str, Any] = {
        "has_gpu": caps.get("has_gpu") is True,
    }
    for key in ("gpu_count", "memory_free_bytes"):
        value = caps.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            hardware[key] = value
    for key in (
        "gpu_names",
        "gpu_compute_capabilities",
        "gpu_memory_total_mib",
        "gpu_memory_free_mib",
    ):
        value = caps.get(key)
        if isinstance(value, list):
            hardware[key] = value[:_MAX_GPU_LIST_ENTRIES]
    cli_inventory = caps.get("cli_inventory")
    if isinstance(cli_inventory, dict):
        hardware["cli_inventory"] = dict(
            list(cli_inventory.items())[:_MAX_CLI_INVENTORY_ENTRIES]
        )
    return hardware
_MAX_CONTAINER_IMAGE_STORE_PATH_CHARS = 512
_MAX_CONTAINER_IMAGE_RECLAIM_REASON_CHARS = 500
_CONTAINER_IMAGE_STORE_RUNTIMES = frozenset({"docker", "podman"})
# Present-image inventory bounds: entry count matches the runner-side cap, and
# a container image reference comfortably fits 256 chars.
_MAX_CONTAINER_IMAGES_PRESENT_ENTRIES = 100
_MAX_CONTAINER_IMAGE_REF_CHARS = 256
_PROJECT_VERIFICATION_VERSION = 1
_MAX_PROJECT_VERIFICATION_REASON_CHARS = 500
_PROJECT_VERIFICATION_BACKENDS = frozenset({"bwrap", "declined"})
_PROJECT_VERIFICATION_BOUNDARY_CLASS = "agent"
# Per-field bound for the runner's compute-interpreter capability report
# (HeartbeatRequest.python_runtime). Its real values are a path, two version
# strings, a provisioner name and one failure sentence, so 300 chars is far
# beyond any legitimate value while bounding what a crafted heartbeat can
# write into the capabilities JSONB column.
_MAX_PYTHON_RUNTIME_FIELD_CHARS = 300
# An idle runner normally completes its self-update within one or two 60s
# retry cycles; an idle drain older than this means the update is failing
# repeatedly (e.g. version hint unreachable from the branch the runner
# tracks) and the runner is silently out of the dispatch pool.
_UPDATE_DRAIN_STUCK_ALERT_SECONDS = 30 * 60


def _sanitize_image_cache_by_project(raw: Any) -> dict | None:
    """Validate/bound a runner-reported image-cache accounting map, or None.

    The runner's report is untrusted display telemetry: coerce every numeric
    field, clamp to sane ceilings, and cap the number of project buckets so a
    malformed or hostile heartbeat can neither store an absurd value nor grow
    the map without bound. Returns None when the payload is absent or unusable.
    """
    if not isinstance(raw, dict):
        return None

    def _clamp_bytes(value: Any) -> int:
        try:
            return max(0, min(_MAX_IMAGE_CACHE_BYTES, int(value)))
        except (TypeError, ValueError):
            return 0

    def _clamp_count(value: Any) -> int:
        try:
            return max(0, min(_MAX_IMAGE_CACHE_BYTES, int(value)))
        except (TypeError, ValueError):
            return 0

    by_project: dict[str, dict] = {}
    raw_by_project = raw.get("by_project")
    if isinstance(raw_by_project, dict):
        for pid, bucket in raw_by_project.items():
            if len(by_project) >= _MAX_IMAGE_CACHE_PROJECTS:
                break
            if not isinstance(pid, str) or not pid.strip() or not isinstance(bucket, dict):
                continue
            by_project[pid.strip()[:128]] = {
                "bytes": _clamp_bytes(bucket.get("bytes")),
                "image_count": _clamp_count(bucket.get("image_count")),
            }
    return {
        "total_bytes": _clamp_bytes(raw.get("total_bytes")),
        "image_count": _clamp_count(raw.get("image_count")),
        "by_project": by_project,
        "unattributed_bytes": _clamp_bytes(raw.get("unattributed_bytes")),
        "unattributed_image_count": _clamp_count(raw.get("unattributed_image_count")),
    }


def _sanitize_container_image_reclaim(raw: Any) -> dict | None:
    """Bound the last runner-side image-store reclaim result for diagnostics."""
    if not isinstance(raw, dict):
        return None

    def _bytes(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return max(0, min(_MAX_IMAGE_CACHE_BYTES, int(value)))
        except (TypeError, ValueError):
            return None

    def _count(value: Any) -> int | None:
        return _bytes(value)

    runtime = raw.get("runtime")
    runtime = runtime if runtime in _CONTAINER_IMAGE_STORE_RUNTIMES else None
    store_root = raw.get("store_root")
    store_root = (
        store_root.strip()[:_MAX_CONTAINER_IMAGE_STORE_PATH_CHARS]
        if isinstance(store_root, str) and store_root.strip()
        else None
    )
    attempted_at = raw.get("attempted_at")
    attempted_at = attempted_at.strip()[:64] if isinstance(attempted_at, str) else None
    reason = raw.get("reason")
    reason = (
        reason.strip()[:_MAX_CONTAINER_IMAGE_RECLAIM_REASON_CHARS]
        if isinstance(reason, str) and reason.strip()
        else None
    )
    return {
        "attempted_at": attempted_at,
        "runtime": runtime,
        "store_root": store_root,
        "free_bytes_before": _bytes(raw.get("free_bytes_before")),
        "free_bytes_after": _bytes(raw.get("free_bytes_after")),
        "removed": _count(raw.get("removed")),
        "kept": _count(raw.get("kept")),
        "in_use_skipped": _count(raw.get("in_use_skipped")),
        "reason": reason,
    }


def _sanitize_resource_capacity(raw: Any) -> dict | None:
    """Normalize bounded, display-only shared-admission telemetry."""
    if not isinstance(raw, dict):
        return None

    def _int(value: Any, limit: int) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return max(0, min(limit, int(value)))
        except (TypeError, ValueError):
            return None

    def _float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            return max(0.0, min(_MAX_RESOURCE_CAPACITY_CORES, float(value)))
        except (TypeError, ValueError):
            return None

    def _section(name: str, keys: tuple[str, ...], parser) -> dict:
        source = raw.get(name)
        source = source if isinstance(source, dict) else {}
        return {key: parser(source.get(key)) for key in keys}

    enabled = raw.get("enabled")
    return {
        "enabled": enabled if isinstance(enabled, bool) else None,
        "reservations": _int(raw.get("reservations"), _MAX_RESOURCE_CAPACITY_COUNT),
        "cpu": _section(
            "cpu", ("total_cores", "reserved_cores", "host_reserved_cores", "free_cores"), _float
        ),
        "memory": _section(
            "memory",
            (
                "total_bytes",
                "reserved_bytes",
                "host_reserved_bytes",
                "observed_free_bytes",
                "free_bytes",
                "min_free_bytes",
            ),
            lambda value: _int(value, _MAX_RESOURCE_CAPACITY_BYTES),
        ),
        "disk": _section(
            "disk",
            ("total_bytes", "reserved_bytes", "observed_free_bytes", "free_bytes", "min_free_bytes"),
            lambda value: _int(value, _MAX_RESOURCE_CAPACITY_BYTES),
        ),
        "gpu": _section("gpu", ("total", "reserved", "free"), lambda value: _int(value, _MAX_RESOURCE_CAPACITY_COUNT)),
    }


def _sanitize_python_runtime(raw: Any) -> dict | None:
    """Normalize the runner's compute-interpreter capability report.

    Untrusted runner input landing in the capabilities JSONB, so only the known
    keys survive and every string is bounded.

    ``usable`` is kept STRICTLY tri-state - ``True`` / ``False`` / ``None``.
    ``None`` means "the runner sent a report whose verdict we cannot read", and
    placement must treat that as UNKNOWN rather than as incapable: coercing an
    unreadable report to False is how a capability gate starves a healthy fleet.
    """
    if not isinstance(raw, dict):
        return None

    def _text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        trimmed = value.strip()[:_MAX_PYTHON_RUNTIME_FIELD_CHARS]
        return trimmed or None

    usable = raw.get("usable")
    return {
        "usable": usable if isinstance(usable, bool) else None,
        "reason": _text(raw.get("reason")),
        "interpreter": _text(raw.get("interpreter")),
        "python_version": _text(raw.get("python_version")),
        "pip_version": _text(raw.get("pip_version")),
        "provisioner": _text(raw.get("provisioner")),
        "probed_at": _text(raw.get("probed_at")),
    }


def _sanitize_project_verification(raw: Any) -> dict | None:
    """Normalize untrusted versioned boundary proof; never trust aggregate ready."""
    if not isinstance(raw, dict):
        return None

    raw_version = raw.get("version")
    version = (
        max(0, min(1_000_000, raw_version))
        if isinstance(raw_version, int) and not isinstance(raw_version, bool)
        else 0
    )
    raw_boundary = raw.get("boundary")
    boundary_class = "unknown"
    boundary_backend = "unknown"
    if isinstance(raw_boundary, dict):
        candidate_class = raw_boundary.get("class")
        if candidate_class == _PROJECT_VERIFICATION_BOUNDARY_CLASS:
            boundary_class = candidate_class
        candidate = raw_boundary.get("backend")
        if (
            isinstance(candidate, str)
            and candidate.strip() in _PROJECT_VERIFICATION_BACKENDS
        ):
            boundary_backend = candidate.strip()

    raw_probes = raw.get("probes")
    raw_probes = raw_probes if isinstance(raw_probes, dict) else {}

    def _probe(name: str) -> dict:
        value = raw_probes.get(name)
        value = value if isinstance(value, dict) else {}
        raw_exit = value.get("exit_code")
        valid_exit_code = (
            isinstance(raw_exit, int)
            and not isinstance(raw_exit, bool)
            and 0 <= raw_exit <= 255
        )
        exit_code = raw_exit if valid_exit_code else None
        passed = value.get("ok") is True and exit_code == 0
        raw_reason = value.get("reason")
        reason = (
            raw_reason.strip()[:_MAX_PROJECT_VERIFICATION_REASON_CHARS]
            if isinstance(raw_reason, str) and raw_reason.strip()
            else None
        )
        if not passed and reason is None:
            reason = (
                f"{name} probe reported an invalid exit code"
                if raw_exit is not None and not valid_exit_code
                else f"{name} probe did not provide zero-exit evidence"
            )
        return {"ok": passed, "exit_code": exit_code, "reason": reason}

    ruff = _probe("ruff")
    docker_postgres = _probe("docker_postgres")
    ready = bool(
        version == _PROJECT_VERIFICATION_VERSION
        and boundary_class == _PROJECT_VERIFICATION_BOUNDARY_CLASS
        and boundary_backend == "bwrap"
        and ruff["ok"]
        and docker_postgres["ok"]
    )
    reasons: list[str] = []
    if version != _PROJECT_VERIFICATION_VERSION:
        reasons.append(f"unsupported project verification version {version}")
    if boundary_class != _PROJECT_VERIFICATION_BOUNDARY_CLASS:
        reasons.append("write-agent boundary class was not agent")
    if boundary_backend != "bwrap":
        reasons.append("write-agent bwrap boundary was not available")
    reasons.extend(
        result["reason"]
        for result in (ruff, docker_postgres)
        if not result["ok"] and result["reason"]
    )
    aggregate_reason = "; ".join(dict.fromkeys(reasons))
    return {
        "version": version,
        "boundary": {"class": boundary_class, "backend": boundary_backend},
        "ready": ready,
        "reason": None
        if ready
        else aggregate_reason[:_MAX_PROJECT_VERIFICATION_REASON_CHARS],
        "probes": {"ruff": ruff, "docker_postgres": docker_postgres},
    }
# _RUNNER_LIVENESS_SECONDS is imported from runner_health_monitors (SSOT) above.

# ComputeJob statuses after which runner reports/uploads must no longer mutate
# the job's evidence (exec stamps, previews): the monitor has finalized — or
# will finalize — from what is already stamped, and verdicts may have been
# drawn from it.
_COMPUTE_JOB_TERMINAL_STATUSES = frozenset(
    {
        ComputeJobStatus.SUCCEEDED,
        ComputeJobStatus.FAILED,
        ComputeJobStatus.CANCELLED,
        ComputeJobStatus.TIMED_OUT,
    }
)

logger = logging.getLogger("blueprint")

router = APIRouter(prefix="/api/runner", tags=["runner"])


def _filter_result_metrics(raw: dict) -> dict:
    """Filter runner-supplied result_metrics to safe, serialisable values.

    Mirrors the per-entry logic in compute_backends._decode_result_metrics:
    - bool → float
    - finite int/float → float
    - "arms" key with a dict or non-empty list → preserved verbatim
    - bounded strings → preserved for diagnostic labels/messages
    - None, non-finite floats, nested objects → dropped
    - capped at _MAX_RUNNER_RESULT_METRICS_KEYS entries
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_RUNNER_RESULT_METRICS_KEYS:
            break
        if not isinstance(key, str):
            continue
        if key == "arms" and isinstance(value, (dict, list)) and value:
            out[key] = value
            continue
        if isinstance(value, bool):
            out[key] = float(value)
        elif isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                continue
            out[key] = numeric
        elif isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) <= _MAX_RUNNER_RESULT_STRING_BYTES:
                out[key] = value
            else:
                out[key] = encoded[:_MAX_RUNNER_RESULT_STRING_BYTES].decode(
                    "utf-8", errors="ignore"
                )
        # None and nested dicts/lists → dropped
    return out


def _filter_progress_record(raw: Any) -> dict | None:
    """Validate ONE self-reported progress record into a fixed-shape dict.

    Progress records originate from sandboxed experiment code (the runner
    relays its BLUEPRINT_PROGRESS_FILE tail), so nothing is trusted: ``metric``
    must be a non-empty bounded string, ``value`` a finite number; ``step``
    (non-negative int) and ``ts`` (bounded string) are optional. Extra keys are
    dropped; anything malformed returns None instead of raising.
    """
    if not isinstance(raw, dict):
        return None
    metric = raw.get("metric")
    value = raw.get("value")
    if not isinstance(metric, str) or not metric.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    record: dict[str, Any] = {
        "metric": metric.strip()[:_MAX_PROGRESS_METRIC_CHARS],
        "value": numeric,
    }
    step = raw.get("step")
    if isinstance(step, int) and not isinstance(step, bool) and 0 <= step <= 10**12:
        record["step"] = step
    ts = raw.get("ts")
    if isinstance(ts, str) and ts:
        record["ts"] = ts[:_MAX_PROGRESS_TS_CHARS]
    return record


def _merge_progress_report(existing: Any, raw: Any, now: datetime) -> tuple[dict | None, list]:
    """Validate a runner progress payload and merge it into the stored state.

    The runner sends ``{"records": [last ≤20 records], "count": total}`` on each
    report. ``count`` deltas against the stored count decide which tail entries
    are NEW (re-sent overlap is not double-appended); the stored list is capped
    at the last :data:`_MAX_PROGRESS_RECORDS_STORED` records. Returns the new
    ``polling_metadata["progress"]`` value ``{"records", "count", "updated_at"}``,
    or None when the payload adds nothing valid — the caller then leaves the
    stored state untouched. Garbage of any shape degrades to None, never a 500.
    """
    if not isinstance(raw, dict):
        return None, []
    raw_records = raw.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        return None, []
    if len(raw_records) > _MAX_PROGRESS_RECORDS_PER_REPORT * 5:
        # Wildly oversized payload: reject wholesale rather than paying to
        # validate an unbounded list (the runner never sends more than 20).
        return None, []
    records = [
        rec
        for rec in (
            _filter_progress_record(item)
            for item in raw_records[-_MAX_PROGRESS_RECORDS_PER_REPORT:]
        )
        if rec is not None
    ]
    if not records:
        return None, []

    prev_records: list = []
    prev_count = 0
    if isinstance(existing, dict):
        stored = existing.get("records")
        if isinstance(stored, list):
            prev_records = stored
        stored_count = existing.get("count")
        if (
            isinstance(stored_count, int)
            and not isinstance(stored_count, bool)
            and stored_count > 0
        ):
            prev_count = min(stored_count, _MAX_PROGRESS_COUNT)

    raw_count = raw.get("count")
    if (
        isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and 0 < raw_count <= _MAX_PROGRESS_COUNT
    ):
        count = raw_count
    else:
        # Invalid/absent count: treat every validated record as new (bounded).
        count = prev_count + len(records)

    delta = count - prev_count
    if delta <= 0:
        # Re-sent snapshot with nothing new — keep the stored state (its
        # updated_at keeps meaning "when the last NEW record arrived").
        return None, []
    new_records = records[-min(delta, len(records)):]
    # Suffix-dedup backstop: a retried report whose count DISAGREES with its
    # validated records (count bumped, tail re-sent verbatim) passes the delta
    # check yet would re-append records already stored. Drop the longest leading
    # run of incoming records that exactly equals the stored tail; the
    # count-delta logic above stays the primary overlap filter.
    if prev_records and new_records:
        for k in range(min(len(new_records), len(prev_records)), 0, -1):
            if prev_records[-k:] == new_records[:k]:
                new_records = new_records[k:]
                break
    if not new_records:
        # Every incoming record was already stored — treat as a re-send.
        return None, []
    merged = (prev_records + new_records)[-_MAX_PROGRESS_RECORDS_STORED:]
    return {
        "records": merged,
        "count": max(count, len(merged)),
        "updated_at": now.isoformat(),  # utc-iso-ok: persisted JSON round-trip, not a wire field
    }, new_records


def _runner_capability_list(capabilities: Dict[str, Any], key: str) -> List[Any]:
    value = capabilities.get(key)
    return value if isinstance(value, list) else []


def _runner_liveness_health(
    *,
    status: str | None,
    last_seen_at: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    if last_seen_at is None:
        seconds_since_last_seen = None
    else:
        seconds_since_last_seen = max(0, int((now - last_seen_at).total_seconds()))

    is_live = (
        status == "active"
        and seconds_since_last_seen is not None
        and seconds_since_last_seen < _RUNNER_LIVENESS_SECONDS
    )
    if is_live:
        device_status = "connected"
        stale_reason = None
    elif status != "active":
        device_status = "offline"
        stale_reason = "inactive" if status == "inactive" else status
    elif seconds_since_last_seen is None:
        device_status = "stale"
        stale_reason = "never_seen"
    else:
        device_status = "stale"
        stale_reason = "heartbeat_stale"

    return {
        "device_status": device_status,
        "is_live": is_live,
        "stale_reason": stale_reason,
    }


def _parse_heartbeat_timestamp(raw: str | None) -> datetime | None:
    """Parse a runner-reported ISO-8601 timestamp to a naive-UTC datetime.

    Tolerant of a trailing ``Z`` and of tz offsets (normalized to UTC then
    stripped, matching the NaiveUTCDateTime column convention). Returns ``None``
    for missing/blank/unparseable values so an old or misbehaving runner never
    fails its heartbeat over a bad timestamp.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def _persist_heartbeat_snapshot(
    session: AsyncSession, *, runner_id: uuid.UUID, caps: Dict[str, Any]
) -> None:
    """Append one RunnerHeartbeatSnapshot row from the just-committed heartbeat.

    Best-effort telemetry history: reads the display-only telemetry the handler
    already merged into ``caps`` and writes a single time-series row. Any failure
    (bad value, DB hiccup) is swallowed and the session rolled back so the
    heartbeat response is never blocked or failed by snapshot persistence — the
    LocalRunner.capabilities dict remains the dispatch source of truth.
    """
    try:
        snapshot = RunnerHeartbeatSnapshot(
            runner_id=runner_id,
            gpu_utilization_pct=caps.get("gpu_utilization_pct"),
            disk_free_bytes=caps.get("disk_free_bytes"),
            memory_free_bytes=caps.get("memory_free_bytes"),
            cpu_load_avg=caps.get("cpu_load_avg"),
            active_task_count=len(caps.get("active_task_ids") or []),
            active_compute_job_count=len(caps.get("active_compute_job_ids") or []),
            agent_available=caps.get("agent_available"),
        )
        session.add(snapshot)
        await session.commit()
    except Exception:
        logger.exception(
            "[RUNNERS] Failed to persist heartbeat snapshot for %s", runner_id
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RUNNERS] rollback after heartbeat-snapshot failure failed for %s",
                runner_id,
            )


class HeartbeatRequest(BaseModel):
    active_task_ids: List[str] = []
    active_compute_job_ids: List[str] = []
    # True iff this heartbeat carries a GENUINE active-work report — the runner's
    # real active_task_ids/active_compute_job_ids as read from its live
    # running-agents table. The daemon heartbeat loop always sets this True; a
    # ``--check`` preflight (which posts active_task_ids=[] as a probe, NOT a
    # report) sets it False. The handler refreshes the reaper-trusted active-set
    # snapshot + active_work_reported_at stamp ONLY when this is True, so a
    # preflight (or any non-report heartbeat) can neither zero out a live task's
    # claim nor mint a fresh stamp over a stale report. Defaults True so an older
    # daemon that omits the field keeps the prior stamp-every-heartbeat behaviour
    # (every loop heartbeat IS a genuine report), while the preflight opts out.
    active_work_report: StrictBool = True
    # True while a version-mismatched daemon drains existing work before its
    # self-update. This is claim availability, not heartbeat liveness.
    update_draining: StrictBool | None = None
    agent_available: StrictBool | None = None
    # Why the agent is unavailable (null when agent_available=True or unknown).
    # COMPOUND, "; "-joined string enumerating EVERY failing leg — the runner no
    # longer stops at the first-matched cause, so a sandbox+no-docker box surfaces
    # all of e.g. "bwrap sandbox unavailable; node runtime not functional; docker
    # agent unavailable" instead of a single opaque phrase.
    agent_unavailable_reason: str | None = None
    # Per-leg ok/failed breakdown of the four independent legs that gate the agent
    # backends: {bwrap_sandbox, agent_dist, node_runtime, docker} -> bool. Optional
    # (old runners omit it); rides in capabilities JSONB so the fleet panel can
    # render a per-leg checklist rather than parse the reason string. Stored as-is
    # when present; None/omitted keeps the prior behaviour byte-for-byte.
    agent_unavailable_legs: dict | None = None
    # Runner-DECLARED agent capability floor (optional; old runners omit it). A
    # docker-only runner (docker+image, NO bwrap) can run only the read-only agent
    # slice and declares those types here so the orchestrator intersects them with
    # the operator-configured `task_types` ceiling. This is a runner CAPABILITY,
    # distinct from operator config — the handler stores it under a separate caps key
    # and never touches `task_types`.
    supported_agent_task_types: list[StrictStr] | None = None
    hostname: str | None = None
    version: str | None = None
    # Clean git commit identity for the checkout serving fleet work. New
    # runners send this on every heartbeat; null means the checkout is not
    # verifiable (for example, it is dirty). Older runners may omit it.
    git_sha: str | None = Field(None, pattern="^[0-9a-f]{7,40}$")
    # Runner-reported isolation-backend inventory + selected backends (optional;
    # old runners omit these). isolation_backends is a flat {backend: bool} map
    # (bwrap_ok/userns_ok/docker_usable/podman_usable/apptainer_ok/enroot_ok/
    # systemd_run_ok); agent_backend/compute_backend name the backend the runner
    # would use for each class. All ride in capabilities JSONB for the fleet panel.
    isolation_backends: dict | None = None
    # Additive functional evidence keyed by the runtime jobs will execute, e.g.
    # {"docker": {"verified": true, "mode": "cdi", "reason": "verified"}}.
    # Old runners omit it and remain heartbeat-compatible.
    container_gpu_passthrough: dict | None = None
    # Whether this host can give a bwrap compute job a Python interpreter with a
    # WORKING pip, plus the evidence behind that verdict. scripts/local_runner.py
    # has been sending BOTH keys on every heartbeat and every --check; pydantic's
    # default extra="ignore" dropped them here, so they never reached
    # runner.capabilities and placement stayed blind to a host that fails every
    # imageless compute job at provisioning time.
    #
    # Route on the NESTED report, never on the flat bool: the runner derives the
    # bool as bool((report or {}).get("usable")), so a host whose boot-time
    # provisioner thread has not published yet reports False while being
    # perfectly capable. Only the nested object separates "probed and failed" (a
    # dict carrying usable=False and a reason) from "not probed yet" (absent).
    python_runtime_usable: StrictBool | None = None
    python_runtime: dict | None = None
    agent_backend: str | None = None
    compute_backend: str | None = None
    # Versioned functional evidence for project-authoring verification. Kept raw
    # here so a future runner version cannot turn the whole liveness heartbeat
    # into a 422; the handler normalizes known v1 fields and fails closed.
    project_verification: Any = None
    # LLM CLI inventory: {"claude": {"present": bool, "version": str}, ...}
    # Probed and sent by the runner each heartbeat; persisted in capabilities JSONB
    # so the orchestrator can filter pool LLM runtimes to clis the runner has.
    llm_clis: dict | None = None
    # Flat CLI inventory: {"claude": "2.1.0", "codex": None, ...} — absent binary
    # ⇒ null, present-with-unknown-version ⇒ "". Derived from the same probe as
    # llm_clis; the flat shape is the capability-dispatch vocabulary.
    cli_inventory: dict | None = None
    # Runner-reported hardware capabilities (optional; old runners omit these).
    has_gpu: StrictBool | None = None
    gpu_count: int | None = None
    gpu_names: List[str] | None = None
    gpu_compute_capabilities: List[float] | None = None
    # Per-GPU VRAM in MiB (index-aligned with gpu_names). Optional: old runners
    # and probe failures omit them; consumers (experiment_authoring_context
    # build_runner_device, future VRAM-fit eligibility) treat absence as
    # "unknown", never as 0.
    gpu_memory_total_mib: List[int] | None = None
    gpu_memory_free_mib: List[int] | None = None
    # Trust telemetry (optional; old runners omit these). last_successful_job_at
    # is ISO-8601; consecutive_job_failures/disk_free_bytes/gpu_utilization_pct are
    # ints. These drive the "connected but failing" degraded state so the operator
    # console can tell a healthy runner from one heartbeating while every job fails.
    last_successful_job_at: str | None = None
    consecutive_job_failures: int | None = None
    # ``disk_free_bytes`` is the legacy workspace measurement. Container-image
    # pulls also report the Docker/Podman data-root separately so admission can
    # gate on the real limiting filesystem.
    disk_free_bytes: int | None = None
    workspace_free_bytes: int | None = None
    container_image_store_runtime: str | None = None
    container_image_store_path: str | None = None
    container_image_store_free_bytes: int | None = None
    container_image_reclaim: dict | None = None
    # Image references already present in the runner's local container image
    # store (``docker image ls`` refs). Dispatch uses this to waive the
    # multi-GB image-pull disk floor when the requested image needs no pull.
    # Best-effort and optional: an older daemon or a failed probe OMITS the
    # field entirely, which means UNKNOWN (the full floor applies) — never an
    # empty store.
    container_images_present: List[str] | None = None
    cpu_load_avg: float | None = None
    memory_free_bytes: int | None = None
    gpu_utilization_pct: int | None = None
    gpu_free_vram_mib: List[int] | None = None
    # Shared host admission telemetry. Optional and display-only: older daemons
    # omit it, while newer daemons report a bounded capacity snapshot.
    resource_capacity: dict | None = None
    # Per-project docker image-cache accounting (optional; only docker-backed
    # runners report it). Shape:
    #   {"total_bytes": int, "image_count": int,
    #    "by_project": {project_id: {"bytes": int, "image_count": int}},
    #    "unattributed_bytes": int, "unattributed_image_count": int}
    # Display-only trust telemetry: an operator can see which allowed project's
    # images fill a multi-project runner's (small) image-layer disk.
    image_cache_by_project: dict | None = None
    # ── Applied-config report (two-way heartbeat config channel) ──────────────
    # The daemon echoes back the LIVE-SAFE config generation it currently has in
    # effect plus the applied live values, so the orchestrator/UI can tell
    # desired-vs-applied apart (config_pending). All optional: an older daemon
    # that never reports these simply shows as pending/unknown, never errors.
    applied_config_generation: str | None = None
    applied_max_concurrent: int | None = None
    applied_max_concurrent_compute: int | None = None
    applied_task_types: List[StrictStr] | str | None = None
    applied_gpu_indices_allowed: List[int] | str | None = None
    applied_gpus_per_job: int | None = None
    # ── Structured config-apply outcome (additive, all optional) ──────────────
    # When a pushed config block cannot be fully applied (unrecognized non-null
    # field), the daemon reports a structured outcome: config_apply_status
    # ("applied"/"rejected"), config_apply_reason (naming the offending
    # field(s)), config_rejected_fields (the offending field name(s) as a
    # structured list), and config_rejected_generation (the generation it could
    # not apply, so the server can distinguish a current failure from a stale one
    # tied to a superseded generation). All None on an older daemon.
    config_apply_status: str | None = None
    config_apply_reason: str | None = None
    config_rejected_fields: List[StrictStr] | None = None
    config_rejected_generation: str | None = None


# ── /next-task work bounds (runtime-configurable) ────────────────────────────
# Two SEPARATE bounds, because they cost wildly different things.
#
# The SCAN bound is how many pinned rows one poll READS. Rows are read WITHOUT a
# lock now, and a candidate with a fresh skip marker is discarded on a dict
# lookup, so scanning is nearly free — and the bound has to be generous, because
# a too-small scan silently re-creates the head-of-line P0 one row further down
# (N+1 poisoned pinned tasks starve the runner again). Escalation
# (``runner_pull_skip``) drains permanently-poisoned tasks out of the scan window,
# so the two work together.
#
# The RESOLVE bound is how many candidates one poll may run credential resolution
# for. THAT is the expensive one: each does a ``Project`` roundtrip and acquires
# the global ``_max_plan_pool_lock``. It is capped far below the scan bound. It
# does not re-introduce head-of-line blocking, because a resolved-and-failed
# candidate gets a fresh skip marker and is short-circuited on the next poll —
# so the resolve budget SLIDES down the candidate list across successive polls
# (seconds apart) instead of restarting at the head every time.
_NEXT_TASK_CANDIDATE_SCAN_DEFAULT = 25
_NEXT_TASK_RESOLVE_ATTEMPTS_DEFAULT = 3


def _next_task_bound(key: str, default: int) -> int:
    """Live work bound from runtime config, clamped so a bad value can't wedge.

    A zero/negative bound would make every poll a no-op (204 forever) and take
    the whole pull-based fleet offline, so it is clamped to >= 1.
    """
    from app.shared_state import runtime_config as _rc

    try:
        value = int((_rc or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _next_task_candidate_scan() -> int:
    return _next_task_bound(
        "runner_next_task_candidate_scan", _NEXT_TASK_CANDIDATE_SCAN_DEFAULT
    )


def _next_task_resolve_attempts() -> int:
    return _next_task_bound(
        "runner_next_task_resolve_attempts", _NEXT_TASK_RESOLVE_ATTEMPTS_DEFAULT
    )


class _ResolvedDispatch:
    """Everything needed to dispatch a task, resolved but not yet APPLIED to it.

    Holding the resolution as a value — rather than stamping the task inside the
    resolver — is what lets credential resolution run OUTSIDE the task-row lock
    (see :func:`next_task`). It also makes abandoning a resolved candidate safe:
    nothing was written, so there is nothing to roll back beyond releasing the
    pool slot the selector reserved (:func:`_release_dispatch_account`).
    """

    __slots__ = (
        "credentials",
        "account_id",
        "provider",
        "auth_mode",
        "model",
        "execution_mode",
    )

    def __init__(
        self,
        credentials: dict | None,
        account_id: str | None = None,
        provider: str | None = None,
        auth_mode: str | None = None,
        model: str | None = None,
        execution_mode: str | None = None,
    ) -> None:
        self.credentials = credentials
        self.account_id = account_id
        self.provider = provider
        self.auth_mode = auth_mode
        self.model = model
        self.execution_mode = execution_mode


def _apply_dispatch_credentials(task: Task, resolved: _ResolvedDispatch) -> None:
    """Stamp the resolved LLM runtime onto the winning task (cost attribution).

    Called ONLY for the candidate that actually wins the row lock, so a candidate
    we resolved and then abandoned never has its context rewritten.
    """
    if resolved.account_id is None:
        return
    from app.max_plan_pool import apply_task_llm_runtime_context

    task.context = apply_task_llm_runtime_context(
        task.context,
        account_id=resolved.account_id,
        provider=resolved.provider,
        auth_mode=resolved.auth_mode,
        model=resolved.model,
        execution_mode=resolved.execution_mode,
    )
    task.llm_account_id = resolved.account_id
    flag_modified(task, "context")


async def _release_dispatch_account(
    resolved: _ResolvedDispatch, project_id: Any
) -> None:
    """Give back the pool slot ``select_llm_runtime_for_task`` pre-incremented.

    The selector reserves capacity on the chosen account the moment it picks one.
    If we then DON'T dispatch that candidate — it lost the winner row-lock race —
    the reservation must be handed back or the poll bleeds pool capacity on every
    lost race. Best-effort: a release failure must never fail the poll.

    ``revert_daily_task_count=True`` because the selector pre-increments BOTH
    ``active_tasks`` AND ``daily_task_count``, and ``_account_at_cap`` gates on
    ``daily_task_count >= daily_task_cap``. A dispatch that never happened must
    not permanently burn a unit of the account's DAILY quota — otherwise every
    lost row-lock race and every credential-resolution retry silently eats the
    day's budget. Matches the canonical peer in ``orchestrator_provision``.
    """
    if resolved.account_id is None:
        return
    try:
        from app.max_plan_pool import release_max_plan_account

        await release_max_plan_account(
            resolved.account_id,
            revert_daily_task_count=True,
            project_id=str(project_id) if project_id else None,
        )
    except Exception:
        logger.exception(
            "[RUNNERS] pool slot release failed for account %s",
            resolved.account_id,
        )


async def _resolve_pool_credentials_for_dispatch(
    task: Task,
    session: AsyncSession,
    runner: LocalRunner,
) -> tuple[bool, _ResolvedDispatch | None, str | None, str | None]:
    """Resolve pooled LLM credentials for dispatching ``task`` to ``runner``.

    Returns ``(ok, resolved, skip_reason, skip_code)``:
    - ``(True, _ResolvedDispatch(credentials={...}, account_id=...), None, None)``
      — account resolved and a pool slot RESERVED on it.
    - ``(True, _ResolvedDispatch(credentials=None), None, None)`` — no runtime
      config / no project row: dispatch with host-env credential fallback
      (pre-pool behavior). No slot reserved.
    - ``(False, None, reason, code)`` — no eligible account, or resolution raised.

    The task row is NEVER mutated here — not on success, not on failure. The
    caller applies the resolution to the winner only (``_apply_dispatch_credentials``)
    and releases the reservation for any candidate it abandons
    (``_release_dispatch_account``). That separation is what allows this — a
    ``Project`` roundtrip plus the global ``_max_plan_pool_lock`` — to run
    OUTSIDE the task-row lock instead of inside it.

    ``code`` separates a genuinely exhausted pool (``no_eligible_account``) from
    a task whose own payload/context crashes credential resolution
    (``credential_resolution_failed``) — the two used to return an identical
    ``{"pool_exhausted": true}`` body, so a poisoned task looked exactly like
    rate limiting and sent operators after the wrong cause.
    """
    task_type = (task.first_payload or {}).get("type", "")
    selected_account_id: str | None = None
    try:
        from app.shared_state import runtime_config as _rc

        _rc_dict = dict(_rc) if _rc else {}
        if not _rc_dict:
            return True, _ResolvedDispatch(credentials=None), None, None
        project = await session.get(Project, task.project_id)
        if project is None:
            return True, _ResolvedDispatch(credentials=None), None, None

        from app.max_plan_pool import (
            reconcile_llm_runtime_identity,
            select_llm_runtime_for_task,
        )
        from app.compute_manifest import (
            _apply_llm_env,
            _resolve_provider_materials,
        )

        # Local claim is an execution-resource boundary just like cloud
        # dispatch: the project's owner pool supplies credentials and capacity.
        dispatch_user_id = await resolve_project_execution_principal(
            session,
            task.project_id,
            project_user_id=project.user_id,
        )

        # Local runners resolve credentials at claim time rather than through
        # the cloud dispatcher. Preserve the same rate-limit and provisioning
        # exclusions here, or a resident retry can be handed the account that
        # just exhausted its quota.
        task_context = task.context or {}
        rate_limited_accounts = task_context.get("blacklisted_accounts") or []
        provision_failed_accounts = (
            task_context.get("provision_failed_accounts") or []
        )
        effective_blacklist = list(
            dict.fromkeys(
                [
                    *(
                        rate_limited_accounts
                        if isinstance(rate_limited_accounts, list)
                        else []
                    ),
                    *(
                        provision_failed_accounts
                        if isinstance(provision_failed_accounts, list)
                        else []
                    ),
                ]
            )
        ) or None
        failure_reasons: list[str] = []
        selection = await select_llm_runtime_for_task(
            user_id=dispatch_user_id,
            task=task,
            runtime_config=_rc_dict,
            blacklist=effective_blacklist,
            project_id=str(task.project_id) if task.project_id else None,
            runner_available_clis=(runner.capabilities or {}).get("llm_clis"),
            failure_reasons=failure_reasons,
        )
        if selection is None or selection.account is None:
            reason = "; ".join(failure_reasons) or "no eligible LLM account"
            logger.warning(
                "[RUNNERS] No LLM account available for task %s (type=%s) — %s",
                task.id,
                task_type,
                reason[:1500],
            )
            return False, None, reason, SKIP_CODE_NO_ACCOUNT

        account = selection.account
        selected_account_id = account.get("id")

        identity = reconcile_llm_runtime_identity(
            account=account,
            account_id=selected_account_id,
            provider=selection.provider,
            auth_mode=selection.auth_mode,
            model=selection.model,
            quality_tier=selection.quality_tier,
            runtime_config=_rc_dict,
        )
        if (
            identity.provider != selection.provider
            or identity.auth_mode != selection.auth_mode
            or identity.model != selection.model
        ):
            logger.warning(
                "[RUNNERS] Reconciled inconsistent runtime identity for task %s: "
                "account=%s provider=%s->%s auth=%s->%s model=%s->%s",
                task.id,
                identity.account_id,
                selection.provider,
                identity.provider,
                selection.auth_mode,
                identity.auth_mode,
                selection.model,
                identity.model,
            )

        provider_settings = selection.settings
        if (
            isinstance(provider_settings, dict)
            and identity.model
            and provider_settings.get("model")
            and provider_settings.get("model") != identity.model
        ):
            provider_settings = {**provider_settings, "model": identity.model}

        # Build credential env/files using the same provider-impl path that
        # cloud-VM manifests use — BEFORE stamping the task, so an exception
        # here leaves the row untouched.
        materials = _resolve_provider_materials(
            oauth_token=account.get("runtime_secret") or account.get("oauth_token"),
            claude_config_json=account.get("claude_config_json"),
            provider=identity.provider,
            auth_mode=identity.auth_mode,
            model=identity.model,
            provider_metadata=account.get("provider_metadata"),
            provider_settings=provider_settings,
        )
        cred_env: dict[str, str] = {
            "PROVIDER": materials.provider_name,
            "AUTH_MODE": materials.auth_mode_value,
        }
        _apply_llm_env(
            cred_env,
            model=identity.model,
            execution_mode=selection.execution_mode,
            provider_settings=provider_settings,
            materials=materials,
        )
        credentials = {
            "env": cred_env,
            "files": [
                {"path": item.path, "content": item.content, "mode": item.mode}
                for item in materials.files
            ],
        }

        logger.info(
            "[RUNNERS] Resolved pooled account %s for task %s (type=%s) on "
            "runner %s (provider=%s model=%s)",
            account.get("id"),
            task.id,
            task_type,
            runner.id,
            identity.provider,
            identity.model,
        )
        # The runtime-context stamp happens in _apply_dispatch_credentials, on
        # the winner only — see this function's docstring.
        return (
            True,
            _ResolvedDispatch(
                credentials=credentials,
                account_id=identity.account_id,
                provider=identity.provider,
                auth_mode=identity.auth_mode,
                model=identity.model,
                execution_mode=selection.execution_mode,
            ),
            None,
            None,
        )
    except Exception as exc:
        logger.exception(
            "[RUNNERS] Failed to resolve LLM credentials for task %s; skipping "
            "candidate so it stays claimable",
            task.id,
        )
        # The selector pre-increments active_tasks AND daily_task_count; release
        # both so a task that repeatedly crashes materials-building doesn't bleed
        # pool capacity or permanently burn the account's DAILY quota on retries
        # (_account_at_cap gates on daily_task_count >= daily_task_cap).
        if selected_account_id is not None:
            try:
                from app.max_plan_pool import release_max_plan_account

                await release_max_plan_account(
                    selected_account_id,
                    revert_daily_task_count=True,
                    project_id=str(task.project_id) if task.project_id else None,
                )
            except Exception:
                logger.exception(
                    "[RUNNERS] pool slot release failed for account %s",
                    selected_account_id,
                )
        return (
            False,
            None,
            f"{type(exc).__name__}: {exc}",
            SKIP_CODE_CREDENTIAL_ERROR,
        )


@router.post("/next-task")
async def next_task(
    response: Response,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Return the highest-priority DISPATCHABLE task pinned to this runner, or 204.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent polls from the same
    runner never double-claim a task. On a hit a task-scoped agent JWT is minted
    and returned; the task stays in PROVISIONING so the agent binary can call
    ``/api/agent/claim`` to perform the PROVISIONING → IN_PROGRESS transition
    (identical contract to GCP-provisioned VMs).

    Candidates are scanned in the SAME order every cloud dispatch path uses
    (priority DESC, then created_at ASC), and a candidate whose LLM account
    selection fails is SKIPPED rather than failing the whole poll. Selecting a
    single candidate and 503-ing on it starved healthy runners indefinitely:
    routing is per-task (task type, explicit model pin, runner CLI inventory),
    so ONE unroutable task at the head — e.g. pinned to a provider in cooldown —
    blocked every routable task behind it, and the head is reaper-exempt
    (``_is_runner_pinned_awaiting_pull``), so nothing ever cleared it.

    Three phases, so that scanning a BATCH costs no more lock-time than the old
    single-candidate poll did:

    1. READ the candidates with NO row lock. Reading is cheap; locking a batch is
       not — holding N task rows ``FOR UPDATE`` across N sequential credential
       resolutions (each a ``Project`` roundtrip AND an acquisition of the global
       ``_max_plan_pool_lock``) would multiply both the row-lock hold time and the
       pool-lock traffic by N, and would only reach that multiple during the
       pool-exhaustion incident this endpoint exists to survive.
    2. RESOLVE credentials outside every lock, for at most
       ``runner_next_task_resolve_attempts`` candidates, skipping any whose
       ``runner_pull_skip`` marker is still fresh (we proved it unroutable seconds
       ago — don't pay the pool lock to prove it again).
    3. LOCK ONLY THE WINNER, re-checking the claim guard (``PROVISIONING`` +
       ``started_at IS NULL``) under ``FOR UPDATE SKIP LOCKED``. That is what
       preserves the no-double-dispatch invariant now that phase 1 is unlocked:
       two concurrent polls may both resolve the same task, but only one can win
       the row, and the loser hands its reserved pool slot back and moves on.
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "next_task")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # A draining runner may finish work it has already pulled, but it must not
    # receive another agent task while the operator is quiescing the host.
    if runner.status != "active" or (runner.capabilities or {}).get(
        "update_draining"
    ) is True:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    pinned_vm_id = f"runner:{runner.id}"
    # Skip markers are scoped to the runner that wrote them: routability depends on
    # the runner's own CLI inventory, so another runner's verdict says nothing about
    # what THIS one can run (a task unpinned from A and re-pinned to B must not
    # inherit A's "unroutable" and be refused by B, which could run it fine).
    runner_id = str(runner.id)
    pull_now = _utcnow()
    # A runner claims work for ANY project on its allow-list (enrollment project
    # UNIONed with its explicit bindings), not only its enrollment project — so one
    # daemon serves N projects. Membership is the server-side authorization boundary:
    # a task pinned to this runner but in a project NOT on the allow-list falls out
    # of the guard and is never returned. The per-runner pin (vm_instance_id +
    # runner_target_id) is unchanged.
    allowed_projects = await allowed_project_ids(session, runner)
    claim_guard = (
        Task.status == TaskStatus.PROVISIONING,
        Task.project_id.in_(allowed_projects),
        Task.vm_instance_id == pinned_vm_id,
        Task.context["runner_target_id"].as_string() == str(runner.id),
        # Exclude tasks already dispatched: started_at is set on dispatch so
        # a re-poll while the agent is booting and calling /api/agent/claim
        # skips this task instead of returning it again (dispatch-storm guard).
        Task.started_at.is_(None),
        # A retry hold means this exact pinned attempt is intentionally asleep.
        # Filter it before credential resolution so polling cannot stamp a skip
        # marker or eventually unpin it, and repeat this predicate in the locked
        # winner query through the shared claim_guard below.
        or_(Task.retry_after_until.is_(None), Task.retry_after_until <= pull_now),
    )

    # ── Phase 1: read candidates, NO row lock ────────────────────────────────
    result = await session.execute(
        select(Task)
        .where(*claim_guard)
        # Same ordering as every cloud dispatch path (orchestrator.py): pulling
        # by created_at alone made the pull-based fleet ignore priority entirely,
        # so a high-priority task pinned to a runner lost to any older one.
        .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
        .limit(_next_task_candidate_scan())
    )
    candidates = list(result.scalars().all())
    if not candidates:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    # ── Phase 2: resolve per-task LLM credentials from the account pool ───────
    # Self-hosted runners receive credential env vars (PROVIDER, AUTH_MODE, and
    # provider-specific secret keys) alongside the task, exactly like cloud VMs
    # get them from their startup-secret manifest.  When no pool account is
    # selected (no accounts configured, or the pool is full/rate-limited), the
    # runner falls back to host env vars — same as the current behavior.
    task: Task | None = None
    credentials: dict | None = None
    skipped: list[dict] = []
    to_escalate: list[tuple[Task, str, str]] = []
    # Deterministic capability mismatches (browser-required task pinned to a runner
    # with no usable Chromium). Collected in the cheap pre-pass and released AFTER
    # the credential loop, alongside to_escalate — releasing takes a row lock, and
    # taking one mid-loop would hold it across the pool-locking credential
    # resolutions this endpoint is written to keep lock-free.
    to_release_capability: list[tuple[Task, str]] = []
    lost_races = 0
    attempts_left = _next_task_resolve_attempts()
    now = _utcnow()

    # The SAME browser contract the dispatch selector consumes, so the pull-time
    # guard and _select_eligible_runner agree exactly on which tasks need Chromium.
    # Lazy: orchestrator_runner_dispatch closes an import cycle with this module.
    from app.orchestrator_runner_dispatch import (
        _runner_lacks_required_browser as _task_runner_lacks_required_browser,
    )
    from app.orchestrator_runner_dispatch import (
        _runner_lacks_required_project_verification as _lacks_project_verification,
    )

    def _runner_lacks_required_browser(candidate: Task) -> bool:
        """This runner cannot satisfy a browser-required candidate pinned to it."""
        return _task_runner_lacks_required_browser(runner, candidate)

    def _runner_lacks_required_project_verification(candidate: Task) -> bool:
        """This runner cannot satisfy a project-verification-required candidate
        pinned to it (no valid v1 write-agent proof for ruff + Docker/Postgres)."""
        return _lacks_project_verification(runner, candidate)

    def _note_skip(candidate: Task, code: str, reason: str) -> None:
        skipped.append(
            {
                "task_id": str(candidate.id),
                "task_type": (candidate.first_payload or {}).get("type", ""),
                "code": code,
                "reason": (reason or "")[:500],
            }
        )

    resolvable: list[Task] = []
    for candidate in candidates:
        # DETERMINISTIC capability mismatch, checked FIRST: a browser-required task
        # was pinned to this runner but the runner reports no usable Chromium. The
        # mismatch is a property of the hardware, not a transient pool cooldown, so
        # re-polling can never route it HERE — release it to QUEUED now instead of
        # stamping a skip and waiting out the ~20-min escalation clock. Normal
        # dispatch already filters browser work to Chromium-capable runners, so the
        # released task re-routes to one (or holds QUEUED until one has a free
        # slot). This backstops every pin path that bypasses the selector's browser
        # gate — direct pins, or a runner that lost the capability after being
        # pinned. Ahead of the freshness short-circuit so a stale marker from an
        # earlier episode can never mask the mismatch.
        if _runner_lacks_required_browser(candidate):
            reason = (
                "runner reports no usable Chromium (playwright_chromium_usable) "
                "but the task requires browser-based acceptance evidence"
            )
            _note_skip(candidate, SKIP_CODE_CAPABILITY_MISMATCH, reason)
            to_release_capability.append((candidate, reason))
            continue
        # Same deterministic mismatch, for the project-verification (Docker
        # publication gate) contract: a task that needs a v1 write-agent proof
        # of ruff + live Docker/Postgres was pinned to a runner that cannot
        # supply one. Also a hardware/registration property, not a transient
        # cooldown — release now rather than stamp a skip and wait out the
        # escalation clock. This backstops the same pin paths as the browser
        # check above (direct pins, or a runner that lost the capability after
        # being pinned).
        if _runner_lacks_required_project_verification(candidate):
            reason = (
                "runner has no valid v1 write-agent proof for python3 -m ruff "
                "and live Docker/Postgres but the task requires project "
                "verification"
            )
            _note_skip(candidate, SKIP_CODE_CAPABILITY_MISMATCH, reason)
            to_release_capability.append((candidate, reason))
            continue
        # Short-circuit: this candidate was proved unroutable seconds ago. Reuse
        # the recorded reason instead of re-acquiring the global pool lock to
        # re-derive it. This is what stops a poll from costing one pool-lock
        # acquisition per pinned task, every few seconds, per runner, for the
        # entire duration of a pool-exhaustion storm.
        if skip_marker_is_fresh(candidate, runner_id, now):
            code, reason = cached_skip(candidate, runner_id)
            _note_skip(candidate, code, reason)
            continue
        resolvable.append(candidate)

    # Resolve order, in two groups:
    #
    #   1. NEVER-EXAMINED first (priority order). This makes the scan window SLIDE:
    #      each poll advances `resolve_attempts` further down the list, so a
    #      routable task at position N is reached in about N/attempts polls. If a
    #      stale re-probe could outrank a never-seen candidate, the poll would
    #      re-prove the head every TTL and never reach position N at all.
    #
    #   2. Then stale re-probes, OLDEST-EXAMINED FIRST — round-robin, NOT priority.
    #      This is the part that is easy to get wrong. In steady state EVERY
    #      candidate carries a marker, so the stale group is the whole list, and
    #      exactly `attempts` markers come due per poll (TTL / poll_interval * ...).
    #      Draining them in PRIORITY order resonates with that cadence and locks the
    #      rotation onto the first `attempts * TTL / poll_interval` candidates —
    #      9, with the defaults — FOREVER. Everything past 9 is examined once and
    #      frozen: never re-probed (so it never dispatches even once it becomes
    #      routable) and its skip count never advances (so it never alerts, never
    #      unpins, never drains) — it holds a candidate slot permanently, and the
    #      effective scan bound silently collapses from 25 to 9. Oldest-first breaks
    #      the resonance: the rotation is fair, every candidate is re-probed, every
    #      counter advances, and the poisoned set actually drains.
    #
    # Losing priority ordering inside group 2 costs nothing real: every member of it
    # has already been proven unroutable (its proof merely EXPIRED — that is what
    # makes it stale rather than fresh), and priority still governs both the scan
    # order and group 1, which is where it decides who actually dispatches first.
    resolve_order = [c for c in resolvable if not has_skip_marker(c, runner_id)]
    resolve_order += sorted(
        (c for c in resolvable if has_skip_marker(c, runner_id)),
        key=lambda c: skip_seen_at(c, runner_id),
    )

    for candidate in resolve_order:
        if attempts_left <= 0:
            # Resolve budget spent. Remaining candidates are simply not examined
            # this poll — the ones we DID examine now carry fresh skip markers, so
            # the next poll short-circuits them and spends its budget further down
            # the list.
            break

        attempts_left -= 1
        ok, resolved, skip_reason, skip_code = (
            await _resolve_pool_credentials_for_dispatch(candidate, session, runner)
        )
        if not ok or resolved is None:
            stamp_skip_reason(
                candidate, skip_code or "", skip_reason or "", runner_id=runner_id
            )
            _note_skip(candidate, skip_code or "", skip_reason or "")
            # Escalation is DEFERRED to after this loop: it takes a row lock (to
            # unpin), and taking one here would hold it across the remaining
            # credential resolutions — each of which acquires the global pool
            # lock. That is the very "row lock held across slow I/O" shape this
            # change exists to remove, so it must not be re-introduced on the
            # escalation path.
            to_escalate.append((candidate, skip_code or "", skip_reason or ""))
            continue

        # ── Phase 3: lock ONLY the winner and re-check the claim guard ────────
        locked = (
            await session.execute(
                select(Task)
                .where(Task.id == candidate.id, *claim_guard)
                .with_for_update(skip_locked=True)
            )
        ).scalars().first()
        if locked is None:
            # Lost the race: a concurrent poll claimed this task (or it moved on)
            # between our unlocked read and now. Hand back the pool slot the
            # selector reserved — otherwise every lost race bleeds capacity — and
            # try the next candidate.
            await _release_dispatch_account(resolved, candidate.project_id)
            lost_races += 1
            continue

        # The episode is over: drop the skip marker. Left in place it is immortal —
        # `count` and `first_at` persist across episodes, so a task that hits a
        # transient cooldown on five SEPARATE occasions (possibly days apart, each
        # recovering on its own) accumulates count=5 and is eventually unpinned for
        # no reason; and `has_skip_marker` would stay True forever, permanently
        # deprioritising a perfectly healthy task in every future resolve order.
        clear_skip_marker(locked)
        _apply_dispatch_credentials(locked, resolved)
        task = locked
        credentials = resolved.credentials
        break

    # ── Escalate skips, now that no credential resolution remains ─────────────
    # A skip counter with no consumer is how the ORIGINAL P0 comes back one row
    # further down: nothing unpins, fails or reaps a permanently-unroutable pinned
    # task, so enough of them silently refill the candidate scan. Alerts at
    # SKIP_ALERT_THRESHOLD; unpins back to QUEUED at SKIP_UNPIN_THRESHOLD so the
    # poisoned set drains instead of being permanent.
    for skipped_task, skip_code, skip_reason in to_escalate:
        await maybe_escalate_pull_skip(
            session, skipped_task, skip_code, skip_reason, runner_id=runner_id
        )

    # ── Release capability mismatches, now that no credential resolution remains ─
    # Deferred for the SAME reason as the escalation above: the release re-locks
    # the row, and holding that lock across the pool-locking credential resolutions
    # is the exact shape this endpoint exists to avoid. A mismatch is deterministic,
    # so this is an immediate unpin (no skip-count clock) — the pin is gone by the
    # end of this poll and normal dispatch re-routes the task to a capable runner.
    for released_task, release_reason in to_release_capability:
        await release_capability_mismatched_task(
            session, released_task, release_reason
        )

    if task is None:
        # Nothing dispatchable. Commit the per-task skip reasons + escalations (so
        # the WHY is visible on each task in the admin UI) and tell the runner WHY,
        # with a code that separates a rate-limited pool from a task that crashes
        # credential resolution — same 503 status either way, so runner backoff
        # is unchanged.
        await session.commit()
        poisoned = [s for s in skipped if s["code"] == SKIP_CODE_CREDENTIAL_ERROR]
        reason_summary = " | ".join(
            f"{s['task_id']} ({s['task_type']}): {s['reason']}" for s in skipped
        )
        if not reason_summary:
            # Every examined candidate was claimed by a concurrent poll between
            # our unlocked read and the winner lock. Not exhaustion — contention.
            # Say so rather than returning a 503 with an empty reason: a blank WHY
            # is the failure mode this whole endpoint is being fixed for.
            reason_summary = (
                f"{lost_races} candidate(s) were claimed concurrently by another "
                "poll; retry shortly"
                if lost_races
                else "no candidate could be examined within this poll's resolve budget"
            )
        logger.warning(
            "[RUNNERS] No dispatchable task for runner %s — all %d pinned "
            "candidate(s) skipped: %s",
            runner.id,
            len(skipped),
            reason_summary[:2000],
        )
        body: dict[str, Any] = {
            # Always present, even when the real cause is a poisoned task:
            # field-deployed runners gate their back-off-and-release-permit
            # branch on this exact key, and dropping it would make an older
            # runner fall through and leak its capacity permit. It now reads as
            # "nothing dispatchable, back off"; the precise cause is below.
            "pool_exhausted": True,
            "reason": reason_summary[:2000],
            "skipped": skipped,
        }
        if poisoned:
            # At least one candidate CRASHED credential resolution rather than
            # merely finding no account — surface that distinctly so an operator
            # fixes the task, not the pool. These were indistinguishable before.
            #
            # Nested under "error", NOT at top level: a bare top-level "task_id"
            # in a body whose sibling is a task payload is one missed check away
            # from an un-upgraded runner treating this 503 as a task to run. Only
            # `pool_exhausted` being checked first makes a flat `task_id` safe,
            # and that is far too load-bearing for a diagnostic field.
            body["error"] = {
                "code": SKIP_CODE_CREDENTIAL_ERROR,
                "skipped_task_id": poisoned[0]["task_id"],
            }
            body["credential_resolution_failed"] = True
        return JSONResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    # Mark as dispatched so re-polls skip this task while the agent boots and
    # calls /api/agent/claim.  The task stays in PROVISIONING so claim can still
    # perform the PROVISIONING → IN_PROGRESS transition (same contract as GCP VMs).
    task_type = (task.first_payload or {}).get("type", "")
    task.started_at = _utcnow()
    session.add(task)
    await session.commit()

    agent_token = create_agent_token(
        str(task.id),
        vm_instance_id=pinned_vm_id,
        task_type=task_type,
        project_id=str(task.project_id),
    )

    return {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "agent_token": agent_token,
        "vm_instance_id": pinned_vm_id,
        "payload": task.payloads,
        "task_type": task_type,
        "resident_session": (
            (task.first_payload or {}).get("resident_session") is True
            or (task.context or {}).get("_resident_session") is True
        ),
        "capability_slice": agent_capability_slice(task_type),
        "credentials": credentials,
    }


class DeclineTaskRequest(BaseModel):
    task_id: str
    # Optional: WHY the runner declined (no bwrap, docker-only host on a write
    # task, agent spawn failure...). Older runners omit it — the endpoint stays
    # backward-compatible and simply records "unspecified".
    reason: str | None = None


@router.post("/decline-task")
async def decline_task(
    payload: DeclineTaskRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Runner declines a previously-dispatched task — release it back to QUEUED.

    A runner may decline a task after claiming it from /next-task if the
    isolation backend cannot build a sandbox for the task type (no bwrap,
    read-only docker image on a write task, etc.).  Without this notification
    the task strands in PROVISIONING until the stale-runner sweep fires — which
    only happens when THIS runner goes offline.

    The decline is RECORDED on the task (``declined_by`` in context, plus the
    reason on ``latest_status_message``). It used to record nothing, which made
    a livelock invisible AND unbreakable: ``_select_eligible_runner``'s tiebreak
    (``r.id.int ^ dispatch_seed``) is stable per task, so with nothing to
    exclude the orchestrator deterministically re-picked the SAME runner that
    just declined — pin → pull → decline, forever, with no counter and no alert.
    ``declined_by`` is what the dispatcher now excludes on, and crossing
    :data:`_DECLINE_ESCALATION_THRESHOLD` raises an operator Alert carrying the
    runner's own reason.
    """
    pinned_vm_id = f"runner:{runner.id}"
    result = await session.execute(
        select(Task).where(
            Task.id == payload.task_id,
            Task.status == TaskStatus.PROVISIONING,
            Task.vm_instance_id == pinned_vm_id,
        )
    )
    task = result.scalars().first()
    if task is None:
        # Task doesn't exist, isn't in PROVISIONING, or isn't pinned to this
        # runner — no pin to release.  Return ok so the runner doesn't retry
        # (idempotent; the stale-runner sweep is the safety net).
        return {"ok": True}

    # Release the LLM pool account that was claimed when the task was
    # dispatched via /next-task, so the slot isn't leaked indefinitely.
    # Use the same idempotent ``_slot_released`` marker as the park path
    # in task_lifecycle.py to guard against double-release.
    acct_id = None
    try:
        from app.max_plan_pool import (
            get_task_llm_account_id,
            release_max_plan_account,
        )

        acct_id = get_task_llm_account_id(task)
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(
                acct_id,
                project_id=str(task.project_id) if task.project_id else None,
            )
    except Exception:
        logger.exception(
            "[RUNNERS] pool account release failed for declined task %s",
            payload.task_id,
        )

    reason = (payload.reason or "").strip() or "unspecified"

    task.llm_account_id = None
    task.status = TaskStatus.QUEUED
    task.vm_instance_id = None
    task.vm_provisioned_at = None
    task.started_at = None
    new_ctx = dict(task.context or {})
    for key in (
        "llm_account_id", "llm_provider", "llm_auth_mode", "llm_model",
        "llm_execution_mode", "max_plan_account_id", "runner_target_id",
        "_slot_released",
    ):
        new_ctx.pop(key, None)

    task.context = new_ctx
    flag_modified(task, "context")
    # ── Record the decline so the dispatcher can stop re-picking this runner ──
    count, total_declines = record_runner_decline(
        task,
        str(runner.id),
        reason=reason,
        hostname=runner.hostname,
    )
    task.latest_status_message = (
        f"Declined by runner {runner.hostname or runner.id} "
        f"(decline #{total_declines}): {reason}"
    )[:500]
    session.add(task)

    # Escalate rather than looping silently: N declines means the pin keeps
    # coming back to hardware that cannot run this task.
    if total_declines >= _DECLINE_ESCALATION_THRESHOLD:
        await _maybe_create_decline_alert(session, task, total_declines, reason)

    await session.commit()

    logger.warning(
        "[RUNNERS] Runner %s declined task %s (decline #%d for this task, #%d "
        "from this runner) — released back to QUEUED (pool account %s released): %s",
        runner.id,
        payload.task_id,
        total_declines,
        count,
        acct_id,
        reason,
    )
    return {"ok": True, "decline_count": total_declines}


async def _acknowledge_authenticated_token_generation(
    session: AsyncSession,
    *,
    runner_id: uuid.UUID,
    authenticated_generation: int,
) -> None:
    """CAS the heartbeat credential acknowledgement against the current row.

    Rotation and heartbeat both update the runner row. The generation predicate
    ensures a heartbeat authenticated before rotation cannot acknowledge the
    replacement credential even if its handler reaches this write afterward.
    """
    await session.execute(
        update(LocalRunner)
        .where(
            LocalRunner.id == runner_id,
            LocalRunner.token_generation == authenticated_generation,
        )
        .values(last_heartbeat_token_generation=authenticated_generation)
        .execution_options(synchronize_session=False)
    )


@router.post("/heartbeat")
async def heartbeat(
    payload: HeartbeatRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Refresh the runner's ``last_seen_at`` and record its active task ids.

    The active-task list is stashed on ``capabilities`` so the orchestrator's
    per-cycle dispatch cache can prefer less-loaded runners without an extra
    per-task query.
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "heartbeat")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # Capture the generation authenticated by get_current_runner. The final
    # compare-and-set below acknowledges it only if rotation has not changed the
    # row's current generation while this request was in flight.
    authenticated_token_generation = runner.token_generation
    previous_status = runner.status
    previous_hostname = runner.hostname
    previous_version = runner.version
    previous_last_seen_at = runner.last_seen_at
    previous_last_successful_job_at = runner.last_successful_job_at
    previous_consecutive_job_failures = runner.consecutive_job_failures
    previous_caps = dict(runner.capabilities or {})
    runner_id = runner.id
    runner_project_id = runner.project_id
    now = _utcnow()
    previous_health = _runner_liveness_health(
        status=previous_status,
        last_seen_at=previous_last_seen_at,
        now=now,
    )

    runner.last_seen_at = now
    # Re-arm an inactive runner on every authenticated heartbeat. The stale-runner
    # sweep flips active->inactive after a single missed 300s window (slept/network-
    # dropped host); a successful heartbeat is proof of liveness, so flip it back to
    # active here. This is the reactivation half of allowing inactive runners to
    # authenticate (see auth.verify_runner_token). Never resurrects a deleted row:
    # a deleted runner can no longer authenticate, so it never reaches this handler.
    # Do NOT re-arm operator-paused or draining runners — they heartbeat to keep
    # liveness telemetry current, but their dispatch status must stay as set.
    if runner.status not in ("paused", "draining"):
        runner.status = "active"
    caps = dict(runner.capabilities or {})
    # Set when THIS heartbeat ends an auto-update drain episode, so the
    # episode's stuck-drain alert is resolved once the row is committed.
    _drain_episode_ended = False
    if payload.update_draining is not None:
        was_update_draining = caps.get("update_draining") is True
        caps["update_draining"] = payload.update_draining
        if payload.update_draining:
            # Recheck on every draining heartbeat, not only the transition.
            # A dispatcher may have built its runner snapshot immediately before
            # the first drain report and pin one final task after that report's
            # release query. Repeating this cheap bounded query closes that race
            # within one heartbeat instead of stranding the late pin.
            released_pins = await release_unpulled_runner_pins(
                session,
                runner.id,
                preserve_active_resident=True,
            )
            if released_pins or not was_update_draining:
                logger.warning(
                    "[RUNNERS] Runner %s %s auto-update drain; "
                    "released_unpulled_pins=%s",
                    runner.id,
                    "entered" if not was_update_draining else "remains in",
                    released_pins,
                )
            # Server-owned stuck-drain detection. The runner's own
            # /update-report failure call is best-effort, swallowed on error,
            # and deduped to one report per distinct failure per daemon
            # lifetime — when the report is lost, an update-wedged runner
            # drains silently forever (fleet-wide dispatch outage 2026-08-17:
            # serving advertised a version hint unreachable from main; every
            # runner looped update-retry every 60s with zero alerts). Track
            # the drain episode here from heartbeats alone and raise one
            # WARNING alert per episode once an IDLE runner has been
            # draining past the threshold — an idle runner normally updates
            # within one heartbeat interval, so a long idle drain means the
            # update keeps failing.
            if not was_update_draining:
                caps["update_draining_since"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
                caps.pop("update_drain_alerted_at", None)
            elif not caps.get("update_draining_since"):
                caps["update_draining_since"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
            try:
                _drain_age_s = (
                    now - datetime.fromisoformat(str(caps["update_draining_since"]))
                ).total_seconds()
            except (TypeError, ValueError):
                caps["update_draining_since"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
                _drain_age_s = 0.0
            if payload.active_work_report:
                _drain_active_work = bool(
                    payload.active_task_ids or payload.active_compute_job_ids
                )
            else:
                _drain_active_work = bool(
                    (caps.get("active_task_ids") or [])
                    or (caps.get("active_compute_job_ids") or [])
                )
            if (
                _drain_age_s > _UPDATE_DRAIN_STUCK_ALERT_SECONDS
                and not _drain_active_work
                and not caps.get("update_drain_alerted_at")
            ):
                caps["update_drain_alerted_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
                from app.main import _current_sha

                # Lazy: app.alert_dispatch imports this router's package.
                from app.alert_dispatch import _update_drain_stuck_dedupe_key

                _runner_label = runner.hostname or f"runner {str(runner_id)[:8]}"
                session.add(
                    Alert(
                        project_id=runner_project_id,
                        severity=AlertSeverity.WARNING,
                        title=f"Runner stuck in update drain: {_runner_label}",
                        message=(
                            f"Self-hosted runner {_runner_label} "
                            f"({str(runner_id)[:8]}) has been idle in "
                            f"auto-update drain for {int(_drain_age_s // 60)} "
                            f"minutes — the self-update is failing repeatedly "
                            f"(runner version {str(runner.version or '?')[:12]}, "
                            f"orchestrator hint {str(_current_sha)[:12]}). The "
                            "runner accepts no claims while draining. Check "
                            "the runner journal; if the hint commit is not "
                            "reachable from the branch the runner tracks "
                            "(main), deploy a main-reachable revision."
                        )[:1000],
                        source="runner",
                        # Literal, not the imported constant: the audience
                        # scanner (test_message_audience) resolves event_type
                        # only from same-module `NAME = "..."` assignments, so
                        # a cross-module import reads as an unregistered type
                        # and the registration in message_audience.py cannot be
                        # verified. Pinned against the shared constant by
                        # test_update_drain_stuck_literal_matches_constant.
                        event_type="runner.update_drain_stuck",
                        # Deterministic per-runner key so the recovery path can
                        # find and dismiss this exact incident. Without it the
                        # row was unfindable and every episode left a permanent
                        # WARNING behind (iconlab 2026-08-20 read 218 minutes
                        # stale while the runner was already healthy).
                        dedupe_key=_update_drain_stuck_dedupe_key(runner_id),
                    )
                )
                logger.error(
                    "[RUNNERS] Runner %s idle in update drain for %.0f min — "
                    "raised runner.update_drain_stuck alert",
                    runner_id,
                    _drain_age_s / 60,
                )
        else:
            # The drain ended. Clearing the episode markers lets the NEXT
            # episode alert from a fresh clock; the episode's own alert is
            # dismissed after the commit below (see _drain_episode_ended)
            # so a recovered runner stops carrying a WARNING that claims its
            # self-update is still failing.
            _drain_episode_ended = was_update_draining
            caps.pop("update_draining_since", None)
            caps.pop("update_drain_alerted_at", None)
    # Refresh the reported active-work set + its report timestamp ONLY for a
    # genuine active-work report. A ``--check`` preflight posts active_task_ids=[]
    # as a probe (active_work_report=False), NOT a real report: writing that empty
    # set with a fresh stamp would let the reaper's zombie-task reconcile conclude
    # a live task is no longer claimed by its runner and reap it. Gating the whole
    # trio together keeps the list and its stamp a coherent pair — a non-report
    # heartbeat leaves the last genuine report untouched.
    #
    # Stamp WHEN this active-work list was reported. last_seen_at alone can't
    # serve: other endpoints bump it without refreshing the list. The reaper's
    # zombie-task reconcile (reaper_orphan_classify._runner_report_excludes_task)
    # only trusts a list carrying a fresh stamp, so a task absent from a fresh
    # report is reapable even while sibling agents keep the shared per-runner
    # vmcontact row alive.
    if payload.active_work_report:
        caps["active_task_ids"] = list(payload.active_task_ids)
        caps["active_compute_job_ids"] = list(payload.active_compute_job_ids)
        caps["active_work_reported_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
    # Merge runner-reported hardware capabilities when present.
    # These override the registration-time default but never clobber
    # operator-configured fields (task_types, max_concurrent).
    if payload.agent_available is not None:
        caps["agent_available"] = payload.agent_available
    if payload.agent_unavailable_reason is not None or payload.agent_available is True:
        caps["agent_unavailable_reason"] = (
            None if payload.agent_available else payload.agent_unavailable_reason
        )
    # Per-leg breakdown ({bwrap_sandbox, agent_dist, node_runtime, docker} -> bool).
    # Additive JSONB, no migration. The runner emits this whenever the leg states
    # are known (even when agent_available=True, where every leg that mattered is
    # ok), so store it as-is; absent/None for old runners that omit it, matching the
    # absent-tolerant contract.
    if payload.agent_unavailable_legs is not None:
        caps["agent_unavailable_legs"] = payload.agent_unavailable_legs
    # Runner-DECLARED agent capability floor. Stored under its own caps key so the
    # orchestrator can intersect it with the operator-configured `task_types`
    # ceiling. CRITICAL: this is a runner capability, NOT operator config — we never
    # write `task_types` here (that stays operator-owned; see the invariant below).
    # Tolerated as absent so older runners keep heartbeating unchanged.
    if payload.supported_agent_task_types is not None:
        caps["supported_agent_task_types"] = list(payload.supported_agent_task_types)
    # Isolation-backend inventory + selected backends. Additive to the JSONB (no
    # migration needed) so the fleet panel can show per-device backend availability.
    # All optional and tolerated as missing so older runners keep heartbeating.
    if payload.isolation_backends is not None:
        caps["isolation_backends"] = payload.isolation_backends
    if payload.container_gpu_passthrough is not None:
        caps["container_gpu_passthrough"] = payload.container_gpu_passthrough
    elif payload.active_work_report:
        # A daemon heartbeat from an older/replaced runner is current negative
        # evidence: retaining proof from a previous process would authorize GPU
        # image work that this daemon cannot prove it can execute. Non-report
        # preflights leave the last genuine daemon inventory untouched.
        caps.pop("container_gpu_passthrough", None)
    # Compute-interpreter capability, persisted from GENUINE daemon heartbeats
    # only. --check runs the probe with provision=False by design ("this probe
    # does not provision"), so an unprovisioned host reports usable=False there
    # while being perfectly able to build the runtime on its next real boot;
    # writing that preflight verdict would make a healthy host permanently
    # ineligible for imageless compute. A preflight therefore neither writes nor
    # clears the last genuine daemon report.
    #
    # A daemon that sends no report (an older runner, or one whose boot-time
    # provisioner has not published yet) CLEARS the keys rather than inheriting
    # the previous process's verdict. That drops the runner back to "unknown",
    # which is the fail-open side of this gate - and it also stops a stale
    # positive from authorizing work the current process cannot prove it can do.
    if payload.active_work_report:
        sanitized_runtime = _sanitize_python_runtime(payload.python_runtime)
        if sanitized_runtime is None:
            caps.pop("python_runtime", None)
            caps.pop("python_runtime_usable", None)
        else:
            caps["python_runtime"] = sanitized_runtime
            # Derived from the sanitized report, never trusted from the wire:
            # the flat bool is fleet-panel telemetry and must not be able to
            # disagree with the evidence placement actually reads.
            caps["python_runtime_usable"] = sanitized_runtime["usable"] is True
    if payload.llm_clis is not None:
        caps["llm_clis"] = payload.llm_clis
    if payload.agent_backend is not None:
        caps["agent_backend"] = payload.agent_backend
    if payload.compute_backend is not None:
        caps["compute_backend"] = payload.compute_backend
    sanitized_verification = _sanitize_project_verification(
        payload.project_verification
    )
    if sanitized_verification is not None:
        caps["project_verification"] = sanitized_verification
    elif payload.active_work_report:
        # A genuine daemon heartbeat without fresh evidence must not retain proof
        # earned by a prior/newer runner binary. Old runners remain heartbeat-
        # compatible but are verification-incapable until they report v1.
        caps.pop("project_verification", None)
    if payload.has_gpu is not None:
        caps["has_gpu"] = payload.has_gpu
    if payload.gpu_count is not None:
        caps["gpu_count"] = payload.gpu_count
    if payload.gpu_names is not None:
        caps["gpu_names"] = payload.gpu_names
    if payload.gpu_compute_capabilities is not None:
        caps["gpu_compute_capabilities"] = [max(0.0, float(v)) for v in payload.gpu_compute_capabilities]
    # Per-GPU VRAM + flat CLI inventory (capability-dispatch vocabulary). All
    # optional and tolerated as absent so older runners keep heartbeating; values
    # are bounded/validated because they feed authoring context and dispatch.
    if payload.gpu_memory_total_mib is not None:
        caps["gpu_memory_total_mib"] = [
            min(_MAX_GPU_MEMORY_MIB, max(0, int(v)))
            for v in payload.gpu_memory_total_mib[:_MAX_GPU_LIST_ENTRIES]
        ]
    if payload.gpu_memory_free_mib is not None:
        caps["gpu_memory_free_mib"] = [
            min(_MAX_GPU_MEMORY_MIB, max(0, int(v)))
            for v in payload.gpu_memory_free_mib[:_MAX_GPU_LIST_ENTRIES]
        ]
    if payload.cli_inventory is not None:
        cli_inventory: dict[str, str | None] = {}
        for name, version in payload.cli_inventory.items():
            if len(cli_inventory) >= _MAX_CLI_INVENTORY_ENTRIES:
                break
            if not isinstance(name, str) or not name.strip():
                continue
            if version is None:
                cli_inventory[name.strip()[:64]] = None
            elif isinstance(version, str):
                cli_inventory[name.strip()[:64]] = version[:_MAX_CLI_VERSION_CHARS]
            # non-str/non-null versions dropped
        caps["cli_inventory"] = cli_inventory
    # Trust telemetry. last_successful_job_at / consecutive_job_failures are
    # first-class columns (the degraded-state logic and panel key on them);
    # disk_free_bytes / gpu_utilization_pct are display-only and ride in caps
    # alongside the other heartbeat-reported hardware fields. All optional and
    # tolerated as missing/null so older runners keep heartbeating.
    parsed_last_success = _parse_heartbeat_timestamp(payload.last_successful_job_at)
    if parsed_last_success is not None:
        runner.last_successful_job_at = parsed_last_success
    if payload.consecutive_job_failures is not None:
        runner.consecutive_job_failures = max(0, int(payload.consecutive_job_failures))
    if payload.disk_free_bytes is not None:
        caps["disk_free_bytes"] = max(0, int(payload.disk_free_bytes))
    if payload.workspace_free_bytes is not None:
        caps["workspace_free_bytes"] = max(0, int(payload.workspace_free_bytes))

    # New runners explicitly send these as ``null`` when Docker/Podman info is
    # unavailable or their active isolation backend changes. That is different
    # from an older runner omitting the field: discard only an explicit-null
    # value, otherwise a transient probe failure would leave a stale low Docker
    # root in capabilities and hold future image jobs indefinitely.
    reported_fields = getattr(payload, "model_fields_set", None)
    if reported_fields is None:  # Pydantic v1 compatibility.
        reported_fields = getattr(payload, "__fields_set__", set())
    if not isinstance(reported_fields, (set, frozenset)):
        reported_fields = set()
    image_store_fields = (
        "container_image_store_runtime",
        "container_image_store_path",
        "container_image_store_free_bytes",
        "container_image_reclaim",
    )
    for field_name in image_store_fields:
        if field_name in reported_fields and getattr(payload, field_name) is None:
            caps.pop(field_name, None)

    # Present-image inventory follows the same explicit-null discipline as the
    # image-store fields above: a reported null clears the stored list so a
    # stale "image present" claim can never waive a real pull floor, while an
    # omitted field (older daemon / probe not run) leaves capabilities alone.
    if (
        "container_images_present" in reported_fields
        and payload.container_images_present is None
    ):
        caps.pop("container_images_present", None)
    if payload.container_images_present is not None:
        images_present: List[str] = []
        for ref in payload.container_images_present:
            if len(images_present) >= _MAX_CONTAINER_IMAGES_PRESENT_ENTRIES:
                break
            if not isinstance(ref, str) or not ref.strip():
                continue
            images_present.append(ref.strip()[:_MAX_CONTAINER_IMAGE_REF_CHARS])
        caps["container_images_present"] = images_present
    if payload.container_image_store_free_bytes is not None:
        caps["container_image_store_free_bytes"] = max(
            0, int(payload.container_image_store_free_bytes)
        )
    if payload.container_image_store_runtime in _CONTAINER_IMAGE_STORE_RUNTIMES:
        caps["container_image_store_runtime"] = payload.container_image_store_runtime
    if (
        isinstance(payload.container_image_store_path, str)
        and payload.container_image_store_path.strip()
    ):
        caps["container_image_store_path"] = (
            payload.container_image_store_path.strip()[:_MAX_CONTAINER_IMAGE_STORE_PATH_CHARS]
        )
    sanitized_reclaim = _sanitize_container_image_reclaim(
        payload.container_image_reclaim
    )
    if sanitized_reclaim is not None:
        caps["container_image_reclaim"] = sanitized_reclaim
    sanitized_image_cache = _sanitize_image_cache_by_project(payload.image_cache_by_project)
    if sanitized_image_cache is not None:
        caps["image_cache_by_project"] = sanitized_image_cache
    if payload.cpu_load_avg is not None:
        caps["cpu_load_avg"] = max(0.0, float(payload.cpu_load_avg))
    if payload.memory_free_bytes is not None:
        caps["memory_free_bytes"] = max(0, int(payload.memory_free_bytes))
    if payload.gpu_utilization_pct is not None:
        caps["gpu_utilization_pct"] = max(0, min(100, int(payload.gpu_utilization_pct)))
    if payload.gpu_free_vram_mib is not None:
        caps["gpu_free_vram_mib"] = [max(0, int(v)) for v in payload.gpu_free_vram_mib]
    sanitized_resource_capacity = _sanitize_resource_capacity(payload.resource_capacity)
    if sanitized_resource_capacity is not None:
        caps["resource_capacity"] = sanitized_resource_capacity
    # Applied-config report. Stored under dedicated caps keys (applied_config /
    # applied_config_generation) so it never clobbers the operator-configured
    # desired subset (max_concurrent, task_types, gpu_indices_allowed) or the
    # hardware-reported telemetry — same merge discipline as every field above.
    # Only written when the daemon reports it, so an older daemon leaves these
    # absent (→ config_pending resolves to True/unknown, never an error).
    if payload.applied_config_generation is not None:
        caps["applied_config_generation"] = payload.applied_config_generation
    applied_report: Dict[str, Any] = {}
    if payload.applied_max_concurrent is not None:
        applied_report["max_concurrent"] = max(0, int(payload.applied_max_concurrent))
    if payload.applied_max_concurrent_compute is not None:
        applied_report["max_concurrent_compute"] = max(
            0, int(payload.applied_max_concurrent_compute)
        )
    if payload.applied_task_types is not None:
        applied_report["task_types"] = (
            "all"
            if payload.applied_task_types == "all"
            else list(payload.applied_task_types)
        )
    if payload.applied_gpu_indices_allowed is not None:
        applied_report["gpu_indices_allowed"] = (
            "all"
            if payload.applied_gpu_indices_allowed == "all"
            else [max(0, int(v)) for v in payload.applied_gpu_indices_allowed]
        )
    if payload.applied_gpus_per_job is not None:
        applied_report["gpus_per_job"] = max(1, int(payload.applied_gpus_per_job))
    if applied_report:
        caps["applied_config"] = applied_report
    # Structured config-apply outcome. Same merge discipline: dedicated caps keys,
    # never clobbering the desired subset or the applied_config echo. Only acted on
    # when the daemon reports a status; an older daemon that omits it leaves any
    # prior keys untouched. On a 'rejected' status we persist the reason, the
    # offending field list (sanitized: list-of-strings, bounded count and length so
    # a malformed report cannot inject unbounded/garbage data), and the rejected
    # generation. On a clean 'applied' status we CLEAR any prior rejection keys so a
    # resolved reason never lingers in the stored row (the view also suppresses a
    # stale reason at read time, so the two mechanisms are complementary).
    if payload.config_apply_status is not None:
        caps["config_apply_status"] = str(payload.config_apply_status)[:32]
        if payload.config_apply_status == "rejected":
            if payload.config_apply_reason is not None:
                caps["config_apply_reason"] = str(payload.config_apply_reason)[:500]
            if payload.config_rejected_fields is not None:
                caps["config_rejected_fields"] = [
                    str(f)[:200] for f in payload.config_rejected_fields[:32]
                ]
            if payload.config_rejected_generation is not None:
                caps["config_rejected_generation"] = str(
                    payload.config_rejected_generation
                )[:128]
        else:
            caps.pop("config_apply_reason", None)
            caps.pop("config_rejected_fields", None)
            caps.pop("config_rejected_generation", None)
    if payload.hostname:
        runner.hostname = payload.hostname[:255]
    if payload.version:
        new_version = payload.version[:64]
        # Stamp the moment a self-update lands (version string changed) so the
        # post-update silent-offline sweep can detect a runner that reported a
        # new version and then went silent — the exact tmux/nohup failure mode
        # where a successful auto-update kills the process with no supervisor to
        # restart it. Only stamped on a genuine change so a steady-state runner
        # never carries a stale "just updated" marker.
        if previous_version is not None and new_version != previous_version:
            caps["version_changed_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
            caps["previous_version"] = previous_version
        runner.version = new_version
    if "git_sha" in payload.model_fields_set:
        runner.source_sha = payload.git_sha
    runner.capabilities = caps
    session.add(runner)
    await session.flush()
    await _acknowledge_authenticated_token_generation(
        session,
        runner_id=runner_id,
        authenticated_generation=authenticated_token_generation,
    )
    await session.commit()
    # Append one time-series snapshot of this heartbeat's telemetry. Best-effort
    # and AFTER the runner commit — the capabilities dict on the LocalRunner row
    # stays the dispatch source of truth; this table is display-only history for
    # the Fleet panel's sparklines and per-runner rollups. A snapshot failure
    # must never fail the heartbeat, so it is fully guarded.
    await _persist_heartbeat_snapshot(session, runner_id=runner_id, caps=caps)
    await refresh_strategic_local_continuation_on_runner_heartbeat(
        session,
        project_id=runner_project_id,
        runner_id=runner_id,
        capabilities=caps,
    )
    current_health = _runner_liveness_health(
        status=runner.status,
        last_seen_at=runner.last_seen_at,
        now=now,
    )
    liveness_recovered = (
        not bool(previous_health.get("is_live"))
        and bool(current_health.get("is_live"))
    )
    runtime_state_changed = (
        liveness_recovered
        or previous_status != runner.status
        or previous_hostname != runner.hostname
        or previous_version != runner.version
        or _runner_capability_list(previous_caps, "active_task_ids")
        != _runner_capability_list(caps, "active_task_ids")
        or _runner_capability_list(previous_caps, "active_compute_job_ids")
        != _runner_capability_list(caps, "active_compute_job_ids")
        or previous_caps.get("agent_available") != caps.get("agent_available")
        or previous_caps.get("has_gpu") != caps.get("has_gpu")
        or previous_caps.get("gpu_count") != caps.get("gpu_count")
        or previous_caps.get("gpu_names") != caps.get("gpu_names")
        or previous_caps.get("gpu_compute_capabilities") != caps.get("gpu_compute_capabilities")
        # Trust-telemetry transitions matter (a runner tipping into/out of the
        # "connected but failing" state), so refetch the panel. gpu_utilization_pct
        # and disk_free_bytes are deliberately excluded — they churn every
        # heartbeat and would defeat the change-gated refetch.
        or previous_last_successful_job_at != runner.last_successful_job_at
        or previous_consecutive_job_failures != runner.consecutive_job_failures
    )
    if runtime_state_changed:
        await emit_workbench_refetch(
            runner.project_id,
            resource="local_runner",
            action="heartbeat",
            refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
            summary_key="runner",
            summary={
                "id": str(runner.id),
                "status": runner.status,
                "active_task_count": len(caps.get("active_task_ids") or []),
                "active_compute_job_count": len(caps.get("active_compute_job_ids") or []),
                "agent_available": caps.get("agent_available"),
            },
        )
    # Recovery: this heartbeat re-armed a previously-inactive runner (the sweep
    # flips active->inactive and raises a runner-deactivated alert). Resolve that
    # open alert so it does not linger falsely now that the runner is live again.
    # Gated on the re-arm transition so a healthy runner's heartbeat never runs
    # the resolve query. Best-effort — a resolve failure must not fail the
    # heartbeat.
    if previous_status != "active":
        try:
            from app.alert_dispatch import _resolve_runner_deactivated_alert

            await _resolve_runner_deactivated_alert(
                session, runner_project_id, runner_id
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve runner-deactivated alert for %s",
                runner_id,
            )
    # Recovery: this heartbeat ended an auto-update drain episode. Gated on the
    # transition, so a healthy runner's heartbeat never runs the resolve query.
    # Best-effort — a resolve failure must not fail the heartbeat.
    if _drain_episode_ended:
        try:
            from app.alert_dispatch import _resolve_update_drain_stuck_alert

            await _resolve_update_drain_stuck_alert(
                session, runner_project_id, runner_id
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to resolve update-drain-stuck alert for %s",
                runner_id,
            )
    # Lazy import to avoid circular dependency (main.py imports this module).
    from app.main import _current_sha

    # Server-authoritative LIVE-SAFE config the daemon may apply mid-flight, plus
    # a deterministic generation marker that changes iff the subset changes. The
    # daemon reconciles this against its applied_config_generation on the next
    # tick and reports back. enrolled_max_concurrent/version_hint are kept for
    # backward compatibility (older daemons read only those).
    live_config = live_safe_config(caps)
    generation = config_generation(live_config)
    return {
        "ok": True,
        "last_seen_at": utc_iso(runner.last_seen_at),
        "enrolled_max_concurrent": caps.get("max_concurrent"),
        "version_hint": _current_sha,
        "config": {**live_config, "config_generation": generation},
        "config_generation": generation,
    }


class RunnerUpdateReportRequest(BaseModel):
    """Report the outcome of a runner self-update.

    Sent by ``blueprint-runner update`` after completion (or failure) so the
    operator console surfaces whether the auto-update succeeded or failed.
    """

    status: str  # "success" or "failure"
    old_version: str | None = None
    new_version: str | None = None


@router.post("/update-report")
async def update_report(
    payload: RunnerUpdateReportRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Accept a runner's self-update outcome report.

    Called best-effort by ``blueprint-runner update`` after the update
    completes (or fails).  The ``runner.self_update`` event is a point-in-time
    notice routed through the shared alert policy.  Its ``TIMELINE_ONLY``
    retirement contract deliberately prevents a durable alert row.

    Authenticated via the runner's bearer token (same as heartbeat).
    Best-effort contract: the runner never blocks its own update CLI on
    this call — any failure is logged and swallowed.
    """
    if payload.status not in ("success", "failure"):
        raise HTTPException(status_code=400, detail="status must be 'success' or 'failure'")

    runner_label = runner.hostname or f"runner {str(runner.id)[:8]}"

    if payload.status == "success":
        severity = AlertSeverity.INFO
        title = f"Runner self-update succeeded: {runner_label}"
        detail = ""
        if payload.old_version and payload.new_version:
            detail = f" {payload.old_version[:10]} → {payload.new_version[:10]}"
        message = (
            f"Self-hosted runner {runner_label} ({str(runner.id)[:8]}) "
            f"auto-updated successfully.{detail}"
        )
    else:
        severity = AlertSeverity.WARNING
        title = f"Runner self-update failed: {runner_label}"
        message = (
            f"Self-hosted runner {runner_label} ({str(runner.id)[:8]}) "
            f"self-update failed — check the runner's logs for details."
        )

    # TIMELINE_ONLY through the policy gate.  "Runner self-update succeeded" is
    # a completed past fact -- no future state makes it false, so nothing could
    # ever resolve it -- and the raw insert carried no dedupe_key of any kind,
    # so every update on every runner left another permanent row in the feed.
    alert, _created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=runner.project_id,
        event_type="runner.self_update",
        severity=severity,
        title=title,
        message=message[:1000],
        source="runner",
    )
    await session.commit()

    if alert is not None:
        await broadcast_to_project(
            str(runner.project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": severity.value,
                "title": alert.title,
                "domain": alert.domain or "core",
                "event_type": alert.event_type,
            },
        )

    return {"ok": True}


class ComputeJobReportRequest(BaseModel):
    exec_status: str  # "running" | "succeeded" | "failed"
    exit_code: int | None = None
    result_metrics: Dict[str, Any] | None = None
    # On terminal reports: the full bounded command log (stamped as
    # runner_command_log). On interim "running" reports: a ~4 KiB live tail
    # (stamped as polling_metadata["live_output"], the GCP live-tail shape).
    command_log: Dict[str, Any] | None = None
    # Optional interim-progress tail {"records": [...], "count": int} from the
    # job's BLUEPRINT_PROGRESS_FILE. Absent from old runners. Typed ``Any`` ON
    # PURPOSE: the tolerance contract lives in _merge_progress_report (garbage
    # of any shape is dropped, never an error) — a Dict annotation would make
    # pydantic 422 scalar garbage before that guard ever runs.
    progress: Any = None
    # Cross-job checkpoint resume provenance: what actually happened when the
    # runner hydrated the predecessor's bundle into the artifact dir before the
    # command ran. Stamped into polling_metadata['resume_provenance'] so the
    # operator can SEE the run resumed (and from where / how much), or that the
    # hydration failed — never claimed when it did not happen.
    resume_hydration: Dict[str, Any] | None = None
    # Explicit durable-delivery outcome from new runners. The command's true
    # exit outcome remains in runner_exec_status; this marker independently says
    # whether its checkpoint bundle reached verified storage.
    artifact_delivery: Dict[str, Any] | None = None
    # Live omission telemetry from the runner's mid-run viewable scan. Typed
    # ``Any`` on purpose: tolerance contract validated inline (garbage is dropped).
    # Shape: {"size_cap": int, "count_cap": int} — counts of unique relpaths skipped
    # due to per-kind size cap or 25-file count cap. Only meaningful on "running"
    # reports; ignored on terminal ones.
    viewable_omissions: Any = None
    # Structured cause for a terminal failure the COMMAND did not cause -- a
    # host-capability gap, a runner-side deadline, a watchdog kill. The runner
    # has been sending this key for as long as ``_report(terminal_reason=...)``
    # has existed in scripts/local_runner.py; until this field was declared,
    # pydantic's default ``extra="ignore"`` DESTROYED it at the API boundary --
    # silently, with no log line. The consequence was not cosmetic: a host that
    # could not produce a Python interpreter reported exit 123 plus "runner
    # provisioning failure: no usable python runtime on this host", the reason
    # never reached the row, ``poll_status`` fell through to the untyped
    # "compute job failed" default, and classify_compute_terminal_reason billed
    # the experiment's AUTHOR a CODE_ERROR -- not budget-exempt, not
    # resurrection-eligible -- for a defect that was entirely in the host. The
    # classifier was always correct; only this transport was missing.
    #
    # Stamped (bounded) into polling_metadata["runner_terminal_reason"] and
    # preferred by RunnerSubprocessBackend.poll_status over the default, so
    # EVERY future runner-side reason works, not just the interpreter one.
    terminal_reason: str | None = None


def _decrypt_registry_auth(auth: dict | None) -> dict | None:
    if not auth or "password" not in auth:
        return auth
    decrypted = dict(auth)
    decrypted["password"] = decrypt_field(auth["password"])
    return decrypted


@router.post("/next-compute-job")
async def next_compute_job(
    response: Response,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Return the oldest STARTING runner-subprocess compute job for this runner's
    project, or 204 if none.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to prevent concurrent polls from
    double-claiming the same job. On a hit the job is atomically transitioned
    STARTING→RUNNING and the exec fields are returned to the runner.
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "next_compute_job")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # Draining is a quiesce state, not a runner failure: already-claimed jobs
    # keep reporting and finish normally, but no new subprocess may be handed to
    # this runner.  Return the ordinary empty-poll response so older runners
    # simply back off while an operator drains the host.
    if runner.status != "active" or (runner.capabilities or {}).get(
        "update_draining"
    ) is True:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    now = _utcnow()
    # Serve compute jobs for ANY project on this runner's allow-list, not only its
    # enrollment project (multi-project runner). Membership is the authorization
    # boundary: a job pinned to this runner in a non-allowed project falls out here.
    allowed_projects = await allowed_project_ids(session, runner)
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.project_id.in_(allowed_projects),
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            ComputeJob.status == ComputeJobStatus.STARTING,
            # Scope to jobs pinned to THIS runner. create_compute_job stamps the
            # pin into polling_metadata['runner_target_id']; without this filter
            # any runner in the project could claim any job — and a non-GPU runner
            # could steal a GPU experiment that _try_runner_dispatch pinned to a
            # has_gpu runner, silently defeating the GPU capability gate. This is
            # the only server-side enforcement point (the response omits the pin,
            # so the runner script cannot self-reject). Mirrors /next-task's
            # context['runner_target_id'] scoping.
            ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
        )
        .order_by(ComputeJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalars().first()
    if job is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    # Claim-time disk-fit re-check for container-image jobs. Server-side dispatch
    # gates on the heartbeat workspace/image-store capacities at PIN time, but
    # either filesystem can fall between pin and this poll. A baked image is
    # multi-GB and must be pulled before the job starts; claiming it on a runner
    # that can no longer fit it wastes the claim. Leave the job STARTING when a
    # known capacity is below the floor. Older runners without this telemetry
    # remain backward-compatible eligible.
    if (job.polling_metadata or {}).get("container_image"):
        from app.orchestrator_runner_dispatch import (
            _container_image_min_free_disk_bytes,
            _container_workspace_min_free_disk_bytes,
        )
        from app.runner_eligibility import (
            _runner_container_image_storage,
            _runner_effective_workspace_disk_floor,
            _runner_has_free_disk,
        )

        image_store_floor = _container_image_min_free_disk_bytes()
        workspace_floor = _container_workspace_min_free_disk_bytes()
        if not _runner_has_free_disk(
            runner,
            image_store_floor,
            workspace_required_bytes=workspace_floor,
        ):
            storage = _runner_container_image_storage(runner)
            effective_workspace_floor = _runner_effective_workspace_disk_floor(
                runner,
                image_store_floor,
                workspace_required_bytes=workspace_floor,
            )
            logger.warning(
                "[RUNNERS] Not dispatching container-image compute job %s to runner "
                "%s: workspace_free_bytes=%s, image_store_free_bytes=%s, "
                "runtime=%s, path=%s; required workspace=%s bytes and image "
                "store=%s bytes; leaving STARTING for re-dispatch",
                job.id,
                runner.id,
                storage["workspace_free_bytes"],
                storage["image_store_free_bytes"],
                storage["image_store_runtime"],
                storage["image_store_path"],
                effective_workspace_floor,
                image_store_floor,
            )
            response.status_code = status.HTTP_204_NO_CONTENT
            return response

    job.status = ComputeJobStatus.RUNNING
    job.started_at = job.started_at or now
    job.last_heartbeat_at = now
    job.updated_at = now
    # Authoritative claim moment: the authenticated runner IS the runner that
    # runs this job. Stamp the durable first-class record of the claiming runner.
    job.ran_on_runner_id = runner.id
    meta = dict(job.polling_metadata or {})
    meta["runner_claimed_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
    job.polling_metadata = meta
    session.add(job)
    await session.commit()
    await session.refresh(job)

    refreshed_meta = job.polling_metadata or {}
    logger.info(
        "[RUNNERS] Dispatched compute job %s to runner %s",
        job.id,
        runner.id,
    )

    # Mint a fresh GitHub token if this job has a repo_clone so the runner can
    # clone the project repo before executing the command.  The token is NOT
    # stored in the DB — it is generated at claim time to avoid persisting a
    # short-lived secret (50-min TTL GitHub installation token).
    repo_clone = refreshed_meta.get("repo_clone") or None
    repo_token: str | None = None
    if repo_clone:
        try:
            from app.branch_cleanup import _get_project_github_token

            # Mint the token for the JOB's project, not the runner's enrollment
            # project — a multi-project runner may be executing a job for any
            # allowed project, and the repo checkout must match that project.
            project = await session.get(Project, job.project_id)
            if project is not None:
                repo_token = await _get_project_github_token(project, session)
        except Exception:
            logger.warning(
                "[RUNNERS] Failed to mint repo token for compute job %s; "
                "runner will execute without a repo checkout",
                job.id,
            )

    task = await session.get(Task, job.task_id) if job.task_id is not None else None
    require_durable_artifact = (
        task is not None
        and (task.first_payload or {}).get("require_durable_artifact") is True
    )
    return {
        "job_id": str(job.id),
        # The job's OWNING project (not the runner's enrollment project — a
        # multi-project runner may run jobs for any allowed project). The runner
        # stamps this into its image-use ledger so the heartbeat can attribute
        # per-project docker image-cache storage back to the right project.
        "project_id": str(job.project_id) if job.project_id else None,
        "command": refreshed_meta.get("command", ""),
        "env": refreshed_meta.get("env") or {},
        "cwd": refreshed_meta.get("cwd"),
        "setup_step": refreshed_meta.get("setup_step"),
        "container_image": refreshed_meta.get("container_image"),
        "container_registry_auth": _decrypt_registry_auth(
            refreshed_meta.get("container_registry_auth")
        ),
        "runtime_deadline_seconds": refreshed_meta.get("runtime_deadline_seconds"),
        "cancel_requested": False,
        "repo_clone": repo_clone,
        "repo_token": repo_token,
        "accelerator_type": job.accelerator_type,
        "allow_host_fallback": refreshed_meta.get("allow_host_fallback", False),
        "no_progress_timeout_seconds": refreshed_meta.get("no_progress_timeout_seconds"),
        # Cross-job checkpoint resume: when set, the runner fetches this
        # predecessor's durable bundle and extracts it into the artifact dir
        # before spawning the subprocess. Validated at create_compute_job time.
        "input_artifact_job_id": refreshed_meta.get("input_artifact_job_id"),
        "require_durable_artifact": require_durable_artifact,
    }


@router.post("/compute-job/{job_id}/report")
async def report_compute_job(
    job_id: uuid.UUID,
    payload: ComputeJobReportRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Runner reports execution status for a claimed compute job.

    Stamps exec_status, exit_code, result_metrics, command_log and (on terminal
    reports) terminal_reason into ``polling_metadata`` so the monitor's existing
    ``poll_status``/finalize path can pick up the terminal state. Does NOT finalize the job itself — the monitor
    owns that transition so artifact-linking and wake delivery run on the normal path.

    Interim ``running`` reports may additionally carry:

    * ``command_log`` — a bounded live tail, stamped as
      ``polling_metadata["live_output"] = {"text", "truncated"}`` (the same
      key/shape the GCP live-tail path writes and the admin UI reads);
    * ``progress`` — the job's self-reported BLUEPRINT_PROGRESS_FILE tail,
      validated (types, finite floats, bounded counts) and merged into
      ``polling_metadata["progress"] = {"records": [{"metric", "value",
      "step"?, "ts"?}, ... last ≤50], "count": total, "updated_at": iso}``.
      Terminal reports may carry ``progress`` too (final records). Malformed
      payloads are dropped, never a 500 — the records originate from sandboxed
      experiment code.

    Idempotent against already-terminal jobs: the update is skipped but the
    endpoint still returns ``{ok, cancel_requested}`` so the runner can observe
    the cancel signal even after the job has been finalized.
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "report_compute_job")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    now = _utcnow()
    # Accept reports for any project on this runner's allow-list (multi-project
    # runner). A job in a project NOT on the allow-list falls out of this filter and
    # is treated as not-found (the cross-project mismatch branch below still fires).
    allowed_projects = await allowed_project_ids(session, runner)
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.id == job_id,
            ComputeJob.project_id.in_(allowed_projects),
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            # Scope to the pinned runner — mirrors the claim filter in
            # /next-compute-job so runner A cannot report on a job claimed by B.
            ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
        )
        .with_for_update()
    )
    job = result.scalars().first()
    if job is None:
        # Check whether the job exists under a project NOT on this runner's
        # allow-list — if so, emit a structured warning so operators can detect
        # misconfigured runners. (A same-allow-list job that merely missed the
        # runner-pin filter is a benign pin mismatch, not a cross-project breach.)
        mismatch_result = await session.execute(
            select(ComputeJob).where(ComputeJob.id == job_id).limit(1)
        )
        mismatch_job = mismatch_result.scalars().first()
        if (
            mismatch_job is not None
            and mismatch_job.project_id not in allowed_projects
        ):
            logger.warning(
                "[RUNNERS] Cross-project access mismatch: runner_id=%s runner_project_id=%s "
                "job_id=%s job_actual_project_id=%s — runner cannot access jobs from another project",
                runner.id,
                runner.project_id,
                job_id,
                mismatch_job.project_id,
            )
            try:
                from app.runner_health_alerts import (
                    _maybe_create_runner_cross_project_access_alert,
                    _record_runner_cross_project_mismatch,
                )

                should_alert, mismatch_count = _record_runner_cross_project_mismatch(
                    runner.id
                )
                if should_alert:
                    await _maybe_create_runner_cross_project_access_alert(
                        session,
                        project_id=runner.project_id,
                        runner_id=runner.id,
                        hostname=runner.hostname,
                        mismatch_count=mismatch_count,
                        attempt_project_id=mismatch_job.project_id,
                    )
            except Exception:
                logger.exception(
                    "[RUNNERS] Failed to create cross-project-access alert for runner %s",
                    runner.id,
                )
        raise HTTPException(status_code=404, detail="compute job not found")

    # Idempotently backfill the authoritative claiming-runner record when still
    # NULL — covers jobs claimed before this column shipped and any claim path
    # that missed the stamp. Never overwrite a non-null value.
    if job.ran_on_runner_id is None:
        job.ran_on_runner_id = runner.id

    meta = dict(job.polling_metadata or {})
    # The monitor finalizes asynchronously after observing this metadata. Once a
    # runner has reported an execution-terminal outcome, keep that outcome
    # latched until finalization: a delayed heartbeat from the runner's interim
    # reporting thread must not roll ``succeeded``/``failed`` back to ``running``.
    # The row lock above serializes competing reports, so this JSONB latch needs
    # no schema migration or client-supplied sequence number.
    latched_runner_status = meta.get("runner_exec_status")
    runner_outcome_latched = latched_runner_status in {
        "succeeded",
        "failed",
    }
    same_terminal_replay = (
        runner_outcome_latched and payload.exec_status == latched_runner_status
    )

    def _report_field_can_fill(key: str) -> bool:
        """Accept first evidence, or fill one missing field on a same-outcome replay."""
        if not runner_outcome_latched:
            return True
        return same_terminal_replay and key not in meta

    runtime_provenance = dict(job.runtime_provenance or {})
    runtime_provenance.update(
        {
            "backend": job.backend,
            "runner_id": str(runner.id),
            "runner_hostname": runner.hostname,
            "runner_version": runner.version,
            "observed_hardware": _observed_runner_hardware(runner),
        }
    )
    job.runtime_provenance = runtime_provenance
    cancel_requested = bool(job.status in _COMPUTE_JOB_TERMINAL_STATUSES)

    # Store a terminal runner log under the common key immediately, before the
    # monitor observes the terminal status.  A cancellation can otherwise win
    # that poll race and strand the only useful stdout/stderr under a runner-only
    # key (or discard a final report altogether).  Keep the legacy key too so
    # older readers remain compatible.
    terminal_runner_log: dict[str, Any] | None = None
    if (
        payload.command_log is not None
        and payload.exec_status != "running"
        and _report_field_can_fill("runner_command_log")
    ):
        if isinstance(payload.command_log, dict):
            log_text = payload.command_log.get("text", "")
            log_truncated = payload.command_log.get("truncated") is True
        elif isinstance(payload.command_log, str):
            log_text = payload.command_log
            log_truncated = False
        else:
            log_text = ""
            log_truncated = False
        if not isinstance(log_text, str):
            log_text = ""
        encoded_log = log_text.encode("utf-8")
        if len(encoded_log) > _MAX_RUNNER_COMMAND_LOG_BYTES:
            log_text = encoded_log[:_MAX_RUNNER_COMMAND_LOG_BYTES].decode("utf-8", "replace")
            log_truncated = True
        terminal_runner_log = {
            "source": "runner",
            "text": log_text,
            "truncated": log_truncated,
        }
        meta["runner_command_log"] = terminal_runner_log
        meta = normalize_terminal_command_log(meta)

    if job.status not in _COMPUTE_JOB_TERMINAL_STATUSES:
        valid_statuses = {"running", "succeeded", "failed"}
        if payload.exec_status in valid_statuses and not runner_outcome_latched:
            meta["runner_exec_status"] = payload.exec_status
        if (
            _report_field_can_fill("runner_exit_code")
            and payload.exit_code is not None
            and isinstance(payload.exit_code, int)
        ):
            meta["runner_exit_code"] = payload.exit_code
        if (
            _report_field_can_fill("runner_terminal_reason")
            and payload.exec_status in ("succeeded", "failed")
            and isinstance(payload.terminal_reason, str)
        ):
            # Terminal-only: a mid-run report has no terminal cause to carry, and
            # accepting one would let a live job pre-stamp the string the
            # classifier keys on. Bounded before it lands in JSONB -- this is
            # runner-supplied, attacker-adjacent input.
            reason_text = payload.terminal_reason.strip()[
                :_MAX_RUNNER_TERMINAL_REASON_CHARS
            ]
            if reason_text:
                meta["runner_terminal_reason"] = reason_text
        if (
            _report_field_can_fill("runner_result_metrics")
            and payload.result_metrics is not None
            and isinstance(payload.result_metrics, dict)
        ):
            meta["runner_result_metrics"] = _filter_result_metrics(payload.result_metrics)
        if (
            not runner_outcome_latched
            and payload.command_log is not None
            and payload.exec_status == "running"
        ):
            # Interim live tail → polling_metadata["live_output"] (fixed shape,
            # bounded). runner_command_log stays terminal-only so the durable
            # full log is never overwritten by a mid-run window.
            if isinstance(payload.command_log, dict):
                live_text = payload.command_log.get("text", "")
                if not isinstance(live_text, str):
                    live_text = ""
                live_truncated = payload.command_log.get("truncated") is True
                encoded = live_text.encode("utf-8")
                if len(encoded) > _MAX_RUNNER_LIVE_OUTPUT_BYTES:
                    live_text = encoded[-_MAX_RUNNER_LIVE_OUTPUT_BYTES:].decode(
                        "utf-8", "replace"
                    )
                    live_truncated = True
                if live_text:
                    meta["live_output"] = {
                        "text": live_text,
                        "truncated": live_truncated,
                    }
        elif payload.command_log is not None:
            # Already normalized before entering the non-terminal branch.  The
            # condition stays for readability: terminal reports never replace a
            # running live tail with an unbounded payload.
            if terminal_runner_log is not None:
                meta["runner_command_log"] = terminal_runner_log
                meta = normalize_terminal_command_log(meta)
        if payload.progress is not None and _report_field_can_fill("progress"):
            merged_progress, new_progress_records = _merge_progress_report(
                meta.get("progress"), payload.progress, now
            )
            if merged_progress is not None:
                meta["progress"] = merged_progress
                for rec in new_progress_records:
                    meta = append_progress_sample(
                        meta,
                        metric=rec["metric"],
                        value=rec["value"],
                        # Runner payload timestamps are sandbox-authored.  They are
                        # useful display data in `progress`, but watchdog timing
                        # must use the server receipt time so a job cannot extend
                        # or backdate its own protection window.
                        t=rec.get("ts") or now.isoformat(),  # utc-iso-ok: persisted JSON round-trip, not a wire field
                        received_at=now,
                    )
        if (
            not runner_outcome_latched
            and payload.exec_status == "running"
            and isinstance(payload.viewable_omissions, dict)
        ):
            omissions: Dict[str, Any] = {}
            for key in ("size_cap", "count_cap"):
                val = payload.viewable_omissions.get(key)
                if isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 10000:
                    omissions[key] = val
            if omissions:
                meta["viewable_live_omissions"] = omissions
        if (
            _report_field_can_fill("runner_finished_at")
            and payload.exec_status in ("succeeded", "failed")
        ):
            meta["runner_finished_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
        if (
            isinstance(payload.resume_hydration, dict)
            and _report_field_can_fill("resume_provenance")
        ):
            # Whitelist known provenance keys so a runner cannot bloat the row.
            raw = payload.resume_hydration
            provenance: Dict[str, Any] = {}
            src = raw.get("resumed_from_job_id")
            expected_src = meta.get("input_artifact_job_id")
            try:
                source_matches = (
                    bool(expected_src)
                    and str(uuid.UUID(str(src))) == str(uuid.UUID(str(expected_src)))
                )
            except (ValueError, AttributeError, TypeError):
                source_matches = False
            if not source_matches:
                raise HTTPException(
                    status_code=422,
                    detail="resume_hydration source does not match job resume source",
                )
            provenance["resumed_from_job_id"] = str(uuid.UUID(str(src)))
            if isinstance(raw.get("ok"), bool):
                provenance["ok"] = raw["ok"]
            for key in ("file_count", "bytes"):
                val = raw.get(key)
                if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                    provenance[key] = val
            reason = raw.get("reason")
            if isinstance(reason, str) and reason.strip():
                provenance["reason"] = reason.strip()[:200]
            if provenance:
                provenance["recorded_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
                meta["resume_provenance"] = provenance
        if (
            payload.exec_status in ("succeeded", "failed")
            and isinstance(payload.artifact_delivery, dict)
            and _report_field_can_fill("artifact_delivery")
        ):
            raw_delivery = payload.artifact_delivery
            delivery: Dict[str, Any] = {}
            status_value = raw_delivery.get("status")
            if status_value in ("delivered", "failed", "not_produced"):
                delivery["status"] = status_value
            protocol = raw_delivery.get("protocol")
            if protocol in ("direct", "legacy", "none"):
                delivery["protocol"] = protocol
            command_status = raw_delivery.get("command_exec_status")
            if command_status in ("succeeded", "failed"):
                delivery["command_exec_status"] = command_status
            if isinstance(raw_delivery.get("verified"), bool):
                delivery["verified"] = raw_delivery["verified"]
            for key in ("file_count", "total_bytes", "archive_size"):
                value = raw_delivery.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    delivery[key] = value
            for key, limit in (("reason", 256), ("crc32c", 16), ("generation", 128)):
                value = raw_delivery.get(key)
                if isinstance(value, str) and value.strip():
                    delivery[key] = value.strip()[:limit]
            if delivery:
                delivery["recorded_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
                meta["artifact_delivery"] = delivery
        job.polling_metadata = meta
        job.last_heartbeat_at = now
        job.updated_at = now
        session.add(job)
        await session.commit()
    elif terminal_runner_log is not None:
        # A late report from the runner is evidence, even when an operator
        # cancellation has already finalized the ComputeJob.  Persist it rather
        # than making a cancelled task hide the completed workload's stdout.
        job.polling_metadata = meta
        job.updated_at = now
        session.add(job)
        await session.commit()
        # The terminal transition may already have created the job-owned
        # observation. Refresh it so a late final stdout/stderr report is not
        # hidden behind a permanently stale document.
        from app.compute_execution_documents import ensure_compute_execution_document

        try:
            await ensure_compute_execution_document(session, job.id)
        except Exception:
            logger.warning(
                "[RUNNERS] Late compute execution document refresh failed for job %s",
                job.id,
                exc_info=True,
            )

    return {"ok": True, "cancel_requested": cancel_requested}


def _parse_multipart_form(body: bytes, content_type: str) -> tuple[dict, dict]:
    """Parse a multipart/form-data body without requiring python-multipart.

    Returns ``(fields, files)``: ``fields`` maps each non-file field name to its
    raw bytes value; ``files`` maps each file field name to its raw bytes content.

    The artifact upload is a small, fixed-shape body (one file part + two scalar
    fields), so a minimal boundary splitter is sufficient — and it avoids adding
    a python-multipart dependency, which FastAPI's ``File``/``Form`` would require
    at route-registration time (a missing dep would crash app boot). Tolerant of
    field ordering and absent parts. Assumes RFC-2046 CRLF delimiters (what httpx
    and the runner client both emit).
    """
    fields: dict[str, bytes] = {}
    files: dict[str, bytes] = {}

    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece[len("boundary="):].strip().strip('"')
            break
    if not boundary:
        return fields, files

    delimiter = b"--" + boundary.encode("latin-1")
    for seg in body.split(delimiter):
        if not seg or seg in (b"--\r\n", b"--", b"\r\n"):
            continue
        if seg.startswith(b"--"):  # trailing closing-boundary remnant
            continue
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        header_end = seg.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_block = seg[:header_end].decode("latin-1", "replace")
        content = seg[header_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]

        name = None
        filename = None
        for hline in header_block.split("\r\n"):
            if hline.lower().startswith("content-disposition:"):
                for param in hline.split(";")[1:]:
                    param = param.strip()
                    if param.startswith("name="):
                        name = param[len("name="):].strip().strip('"')
                    elif param.startswith("filename="):
                        filename = param[len("filename="):].strip().strip('"')
        if name is None:
            continue
        if filename is not None:
            files[name] = content
        else:
            if len(content) > _MAX_MULTIPART_FIELD_BYTES:
                raise ValueError(
                    f"non-file field {name!r} exceeds {_MAX_MULTIPART_FIELD_BYTES} byte limit"
                )
            fields[name] = content
    return fields, files


class _FileRangeReader:
    """Bounded, forward-only reader over a byte range of a seekable file.

    Presents ``[start, start + length)`` of ``fileobj`` as its own read stream so
    the (potentially multi-hundred-MiB) file part of a spooled multipart body can
    be streamed to GCS via ``blob.upload_from_file`` WITHOUT ever buffering it in
    the orchestrator heap. A single consumer reads it sequentially; not
    thread-safe (but the resumable upload runs on one worker thread).
    """

    def __init__(self, fileobj, start: int, length: int):
        self._f = fileobj
        self._remaining = max(0, length)
        self._f.seek(start)

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._f.read(want)
        self._remaining -= len(data)
        return data


def _parse_multipart_form_stream(
    fileobj,
    content_type: str,
    *,
    read_chunk: int = 1024 * 1024,
    header_window: int = 64 * 1024,
    max_parts: int = _MAX_MULTIPART_PARTS,
    max_field_bytes: int = _MAX_MULTIPART_FIELD_BYTES,
) -> tuple[dict, dict | None]:
    """Memory-bounded multipart parse over a SEEKABLE binary file.

    The streaming counterpart of :func:`_parse_multipart_form`: instead of
    slicing an in-memory ``bytes`` body (which forces the whole payload — up to a
    512 MiB video — onto the heap), it locates each part by scanning the file for
    boundary delimiters in a bounded sliding window, so the large file part is
    never loaded into memory. Scalar fields (always small for this endpoint) are
    read into memory as bytes; the single file part is returned as a byte-range
    descriptor ``{"name", "filename", "start", "length"}`` the caller streams via
    :class:`_FileRangeReader`.

    Returns ``(fields, file_part)`` where ``file_part`` is ``None`` when no file
    part is present. Mirrors the leniency of the in-memory parser (tolerant of
    field ordering and absent parts; assumes RFC-2046 CRLF delimiters).
    """
    fields: dict[str, bytes] = {}
    file_part: dict | None = None

    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            boundary = piece[len("boundary="):].strip().strip('"')
            break
    if not boundary:
        return fields, None

    delimiter = b"--" + boundary.encode("latin-1")
    dlen = len(delimiter)

    # ── Pass 1: find every delimiter offset with a bounded sliding window ──
    positions: list[int] = []
    fileobj.seek(0)
    overlap = dlen - 1
    carry = b""
    base = 0
    while True:
        chunk = fileobj.read(read_chunk)
        if not chunk:
            break
        buf = carry + chunk
        search_from = 0
        while True:
            idx = buf.find(delimiter, search_from)
            if idx == -1:
                break
            positions.append(base + idx)
            if len(positions) > max_parts:
                raise ValueError(
                    f"multipart body contains more than {max_parts} boundary markers"
                )
            search_from = idx + dlen
        keep = min(len(buf), overlap)
        base += len(buf) - keep
        carry = buf[len(buf) - keep:] if keep else b""

    # ── Pass 2: for each segment between delimiters, read its (small) header ──
    for i in range(len(positions) - 1):
        seg_start = positions[i] + dlen
        seg_end = positions[i + 1]
        if seg_start >= seg_end:
            continue
        fileobj.seek(seg_start)
        window = fileobj.read(min(header_window, seg_end - seg_start))
        if window.startswith(b"--"):  # closing-boundary remnant
            continue
        header_off = 2 if window.startswith(b"\r\n") else 0
        header_rel = window.find(b"\r\n\r\n", header_off)
        if header_rel == -1:
            continue
        header_block = window[header_off:header_rel].decode("latin-1", "replace")
        content_start = seg_start + header_rel + 4
        content_end = seg_end
        if content_end - content_start >= 2:  # strip CRLF before next delimiter
            content_end -= 2

        name = None
        filename = None
        for hline in header_block.split("\r\n"):
            if hline.lower().startswith("content-disposition:"):
                for param in hline.split(";")[1:]:
                    param = param.strip()
                    if param.startswith("name="):
                        name = param[len("name="):].strip().strip('"')
                    elif param.startswith("filename="):
                        filename = param[len("filename="):].strip().strip('"')
        if name is None:
            continue
        length = max(0, content_end - content_start)
        if filename is not None:
            if file_part is None:  # first file part wins (endpoint sends one)
                file_part = {
                    "name": name,
                    "filename": filename,
                    "start": content_start,
                    "length": length,
                }
        else:
            if length > max_field_bytes:
                raise ValueError(
                    f"non-file field {name!r} exceeds {max_field_bytes} byte limit"
                )
            fileobj.seek(content_start)
            fields[name] = fileobj.read(length)

    return fields, file_part


class ArtifactTransferAuthorizeRequest(BaseModel):
    archive_size: int = Field(gt=0)
    crc32c: StrictStr
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class ArtifactTransferCompleteRequest(BaseModel):
    transfer_id: uuid.UUID
    generation: StrictStr


async def _finalize_artifact_transfer_failure(
    session: AsyncSession,
    scope: tuple,
    transfer_id: uuid.UUID,
    reason: str,
    *,
    expected_status: str,
) -> None:
    """Fail a current transfer only from the state that initiated this operation."""
    result = await session.execute(
        select(ComputeJob)
        .where(*scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:
        return
    meta = dict(job.polling_metadata or {})
    current = meta.get("runner_artifact_transfer")
    if (
        not isinstance(current, dict)
        or current.get("transfer_id") != str(transfer_id)
        or current.get("status") != expected_status
    ):
        return
    meta["runner_artifact_transfer"] = {
        **current,
        "status": "failed",
        "failure_reason": reason,
        "failed_at": _utcnow().isoformat(),
    }
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()


@router.post("/compute-job/{job_id}/artifact-transfer")
async def authorize_compute_job_artifact_upload(
    job_id: uuid.UUID,
    payload: ArtifactTransferAuthorizeRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Authorize a large bundle to bypass Cloud Run and upload to exact GCS object."""
    allowed, retry_after = await check_runner_rate_limit(
        str(runner.id), "authorize_compute_job_artifact_upload"
    )
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})
    checksum = payload.crc32c.strip()
    try:
        decoded_checksum = base64.b64decode(checksum, validate=True)
    except (binascii.Error, ValueError):
        decoded_checksum = b""
    if len(decoded_checksum) != 4:
        raise HTTPException(status_code=422, detail="crc32c must be a non-empty base64 checksum")

    allowed_projects = await allowed_project_ids(session, runner)
    scope = (
        ComputeJob.id == job_id,
        ComputeJob.project_id.in_(allowed_projects),
        ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
        ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
    )
    result = await session.execute(select(ComputeJob).where(*scope).with_for_update())
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")
    meta = dict(job.polling_metadata or {})
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES and meta.get("runner_artifact") is not None:
        raise HTTPException(status_code=409, detail="compute job is finalized — its artifact evidence is immutable")
    transfer_id = uuid.uuid4()
    expires_at = _utcnow() + timedelta(seconds=_ARTIFACT_TRANSFER_TTL_SECONDS)
    meta["runner_artifact_transfer"] = {
        "transfer_id": str(transfer_id),
        "archive_size": payload.archive_size,
        "crc32c": checksum,
        "file_count": payload.file_count,
        "total_bytes": payload.total_bytes,
        "expires_at": expires_at.isoformat(),
        "status": "authorizing",
    }
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    # IAM credential refresh/signBlob can involve network I/O.  The attempt is
    # reserved above, but no row lock or transaction is held while it runs.
    try:
        authorization = await app_storage.authorize_compute_job_artifact_transfer(
            job_id, transfer_id, expires_at=expires_at
        )
    except Exception as exc:
        logger.warning("Artifact transfer authorization failed for %s", job_id, exc_info=True)
        await _finalize_artifact_transfer_failure(
            session,
            scope,
            transfer_id,
            "authorization_failed",
            expected_status="authorizing",
        )
        raise HTTPException(status_code=503, detail="artifact persistence authorization failed") from exc

    result = await session.execute(
        select(ComputeJob)
        .where(*scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")
    meta = dict(job.polling_metadata or {})
    current = meta.get("runner_artifact_transfer")
    if not isinstance(current, dict) or current.get("transfer_id") != str(transfer_id):
        raise HTTPException(status_code=409, detail="artifact transfer was superseded")
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES and meta.get("runner_artifact") is not None:
        meta["runner_artifact_transfer"] = {
            **current,
            "status": "failed",
            "failure_reason": "evidence_finalized",
            "failed_at": _utcnow().isoformat(),
        }
        job.polling_metadata = meta
        job.updated_at = _utcnow()
        session.add(job)
        await session.commit()
        raise HTTPException(status_code=409, detail="compute job is finalized — its artifact evidence is immutable")
    meta["runner_artifact_transfer"] = {**current, "status": "authorized"}
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()
    return {"ok": True, "transfer_id": str(transfer_id), **authorization}


@router.post("/compute-job/{job_id}/artifact-transfer/complete")
async def complete_compute_job_artifact_upload(
    job_id: uuid.UUID,
    payload: ArtifactTransferCompleteRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Verify direct-upload metadata before stamping durable runner evidence."""
    allowed, retry_after = await check_runner_rate_limit(
        str(runner.id), "complete_compute_job_artifact_upload"
    )
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})
    allowed_projects = await allowed_project_ids(session, runner)
    scope = (
        ComputeJob.id == job_id,
        ComputeJob.project_id.in_(allowed_projects),
        ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
        ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
    )
    result = await session.execute(select(ComputeJob).where(*scope))
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")
    pending = (job.polling_metadata or {}).get("runner_artifact_transfer")
    if not isinstance(pending, dict) or pending.get("transfer_id") != str(payload.transfer_id):
        raise HTTPException(status_code=409, detail="artifact transfer is not current")
    existing = (job.polling_metadata or {}).get("runner_artifact")
    if pending.get("status") == "verified" and isinstance(existing, dict):
        if (
            existing.get("transfer_id") == str(payload.transfer_id)
            and existing.get("generation") == payload.generation
        ):
            return {"ok": True, "runner_artifact": existing}
        raise HTTPException(status_code=409, detail="artifact evidence is immutable")
    if pending.get("status") != "authorized":
        raise HTTPException(status_code=409, detail="artifact transfer is not completable")
    try:
        verified = await app_storage.verify_compute_job_artifact_transfer(job_id, payload.transfer_id)
    except Exception as exc:
        logger.warning("Artifact transfer verification failed for %s", job_id, exc_info=True)
        await _finalize_artifact_transfer_failure(
            session,
            scope,
            payload.transfer_id,
            "verification_unavailable",
            expected_status="authorized",
        )
        raise HTTPException(status_code=503, detail="artifact persistence verification failed") from exc
    if (
        verified["size"] != pending.get("archive_size")
        or verified.get("crc32c") != pending.get("crc32c")
        or verified.get("generation") != payload.generation
    ):
        await _finalize_artifact_transfer_failure(
            session,
            scope,
            payload.transfer_id,
            "metadata_mismatch",
            expected_status="authorized",
        )
        raise HTTPException(status_code=409, detail="artifact size, checksum, or generation verification failed")

    result = await session.execute(
        select(ComputeJob).where(*scope).with_for_update().execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")
    meta = dict(job.polling_metadata or {})
    current = meta.get("runner_artifact_transfer")
    if not isinstance(current, dict) or current.get("transfer_id") != str(payload.transfer_id):
        raise HTTPException(status_code=409, detail="artifact transfer was superseded")
    existing = meta.get("runner_artifact")
    if current.get("status") == "verified" and isinstance(existing, dict):
        if (
            existing.get("transfer_id") == str(payload.transfer_id)
            and existing.get("generation") == payload.generation
        ):
            return {"ok": True, "runner_artifact": existing}
        raise HTTPException(status_code=409, detail="artifact evidence is immutable")
    if current.get("status") != "authorized":
        raise HTTPException(status_code=409, detail="artifact transfer is not completable")
    if existing is not None:
        if existing.get("generation") == verified["generation"] and existing.get("uri") == verified["uri"]:
            return {"ok": True, "runner_artifact": existing}
        raise HTTPException(status_code=409, detail="artifact evidence is immutable")
    artifact_info = {
        "kind": "runner_artifact",
        "uri": verified["uri"],
        "file_count": current["file_count"],
        "total_bytes": current["total_bytes"],
        "stored": True,
        "verified": True,
        "size": verified["size"],
        "crc32c": verified["crc32c"],
        "generation": verified["generation"],
        "transfer_id": str(payload.transfer_id),
    }
    meta["runner_artifact"] = artifact_info
    meta["runner_artifact_transfer"] = {**current, "status": "verified", "verified_at": _utcnow().isoformat()}  # utc-iso-ok: persisted JSON round-trip, not a wire field
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()
    return {"ok": True, "runner_artifact": artifact_info}


@router.post("/compute-job/{job_id}/artifact")
async def upload_compute_job_artifact(
    job_id: uuid.UUID,
    request: Request,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Receive a runner's durable artifact tarball for a claimed compute job.

    Body is multipart/form-data: file field ``archive`` (a .tar.gz) plus form
    fields ``file_count`` and ``total_bytes``. Enforces the 25 MiB v1 cap (413 if
    exceeded), stores the archive via ``compute_backends.store_runner_artifact``,
    and links the returned descriptor into ``polling_metadata['runner_artifact']``
    for the monitor/finalize path. Scoped to the pinned runner exactly like the
    report endpoint, so runner A cannot upload to a job claimed by runner B.
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "upload_compute_job_artifact")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # Early-reject obviously-oversized uploads before buffering the whole body.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        if declared is not None and declared > _MAX_RUNNER_ARTIFACT_BYTES + _MULTIPART_BODY_MARGIN:
            raise HTTPException(status_code=413, detail="artifact exceeds 25 MiB upload cap")

    # Buffer the body incrementally with a hard cap on ACTUAL bytes received.
    # The Content-Length pre-check above is advisory only — a chunked
    # transfer-encoding request carries no Content-Length at all — so the cap
    # must bind on the stream itself before any parsing or storage work.
    max_body_bytes = _MAX_RUNNER_ARTIFACT_BYTES + _MULTIPART_BODY_MARGIN
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_body_bytes:
            raise HTTPException(status_code=413, detail="artifact exceeds 25 MiB upload cap")
        chunks.append(chunk)
    raw = b"".join(chunks)
    fields, files = _parse_multipart_form(raw, request.headers.get("content-type", ""))

    archive_bytes = files.get("archive")
    if archive_bytes is None:
        raise HTTPException(status_code=400, detail="missing 'archive' file field")
    if len(archive_bytes) > _MAX_RUNNER_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="artifact exceeds 25 MiB upload cap")

    def _to_int(val: bytes | None) -> int:
        try:
            return int((val or b"0").decode("latin-1").strip())
        except (ValueError, AttributeError):
            return 0

    file_count = _to_int(fields.get("file_count"))
    total_bytes = _to_int(fields.get("total_bytes"))

    # Same ownership scoping as the report endpoint: pinned runner only.
    # Validation runs on a PLAIN read — the GCS upload below can take seconds,
    # and holding the ComputeJob row FOR UPDATE across slow I/O is the exact
    # lock-held-across-slow-I/O pattern behind the task-row-contention
    # incidents. The row lock is taken only for the brief re-read + stamp
    # after the bytes are durably stored.
    allowed_projects = await allowed_project_ids(session, runner)
    _job_scope = (
        ComputeJob.id == job_id,
        ComputeJob.project_id.in_(allowed_projects),
        ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
        ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
    )
    result = await session.execute(select(ComputeJob).where(*_job_scope))
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")

    # TERMINAL-STATUS GUARD — the promise _COMPUTE_JOB_TERMINAL_STATUSES makes at
    # the top of this module ("runner reports/uploads must no longer mutate the
    # job's evidence") was enforced on /report but NOT here, so a pinned runner
    # could POST to a SUCCEEDED/FAILED job and silently REPLACE its durable
    # evidence: store_runner_artifact writes to the fixed per-job object
    # `compute-jobs/<job_id>/artifacts/bundle.tar.gz`, so a late upload
    # overwrites the bundle a verdict may already have been drawn from.
    #
    # The guard is scoped precisely to that overwrite, and NOT to "any upload to
    # a terminal job", because a terminal-but-unstamped job is the NORMAL race:
    # the runner sends its terminal /report FIRST and uploads the artifact
    # afterwards (local_runner.py finalizes in a `finally:` block), while the
    # monitor finalizes the job the moment it sees runner_exec_status=succeeded.
    # A first artifact landing just after that flip is the common case, adds
    # evidence rather than mutating it, and must still be accepted — rejecting it
    # would silently destroy the artifacts of every job whose upload lost that
    # race. What must never happen is a SECOND upload replacing evidence that is
    # already stamped on a finalized job.
    existing_artifact = (job.polling_metadata or {}).get("runner_artifact")
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES and existing_artifact is not None:
        logger.warning(
            "[RUNNERS] Rejected artifact overwrite for FINALIZED compute job %s "
            "(status=%s) from runner %s — durable evidence is immutable once the "
            "job is terminal",
            job_id,
            job.status,
            runner.id,
        )
        raise HTTPException(
            status_code=409,
            detail="compute job is finalized — its artifact evidence is immutable",
        )

    # store_runner_artifact is implemented in parallel in app.compute_backends
    # (pinned contract). Imported locally — same pattern as the repo-token import
    # in /next-compute-job — so module import / app boot never depends on its
    # presence at import time.
    from app.compute_backends import store_runner_artifact

    # Slow storage upload with NO row lock held (uses only job.id).
    artifact_info = await store_runner_artifact(
        session, job, archive_bytes, file_count, total_bytes
    )
    if not artifact_info.get("stored"):
        raise HTTPException(status_code=503, detail="artifact persistence failed")

    # Brief lock: re-read the FRESH row, then stamp. populate_existing refreshes
    # the identity-map instance so the stamp merges over anything a concurrent
    # writer committed during the unlocked storage window.
    result = await session.execute(
        select(ComputeJob)
        .where(*_job_scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:  # deleted mid-upload; object is orphaned, best-effort
        raise HTTPException(status_code=404, detail="compute job not found")

    meta = dict(job.polling_metadata or {})

    # Re-check the guard under the lock. The plain read above happened BEFORE a
    # storage upload that can take seconds; in that window the job can finalize
    # AND a concurrent uploader can stamp its own artifact. Re-pointing a
    # finalized job's evidence at ours here would be exactly the mutation the
    # pre-check refuses, just through a narrower door — so refuse it too rather
    # than clobbering the stamp a verdict may already have read.
    if (
        job.status in _COMPUTE_JOB_TERMINAL_STATUSES
        and meta.get("runner_artifact") is not None
        and existing_artifact is None
    ):
        logger.warning(
            "[RUNNERS] Compute job %s finalized and was stamped by a concurrent "
            "uploader while runner %s's artifact was in flight — keeping the "
            "stamped evidence, discarding the late pointer",
            job_id,
            runner.id,
        )
        raise HTTPException(
            status_code=409,
            detail="compute job is finalized — its artifact evidence is immutable",
        )

    meta["runner_artifact"] = artifact_info
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    # For the first late bundle upload on a terminal job, backfill the artifact
    # linkage that the terminal transition missed because the bundle had not yet
    # arrived. Without this call the job retains runner_artifact metadata but
    # artifact_id / result_reference remain NULL, so the canvas loses the result
    # once live preview synthesis stops on terminal status.
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES and existing_artifact is None:
        try:
            from app.compute_jobs import _link_terminal_artifact

            await _link_terminal_artifact(session, job)
            await session.commit()
        except Exception:
            logger.warning(
                "[RUNNERS] Late bundle linkage failed for terminal job %s — "
                "artifact_id not backfilled",
                job_id,
                exc_info=True,
            )

    return {"ok": True, "runner_artifact": artifact_info}


@router.get("/compute-job/{job_id}/input-artifact")
async def download_compute_job_input_artifact(
    job_id: uuid.UUID,
    direct: bool = False,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Stream the predecessor checkpoint bundle for a claimed resume job.

    Cross-job checkpoint resume: ``{job_id}`` is the CURRENT job the runner is
    executing (pinned to this runner). Its ``polling_metadata`` carries the
    validated ``input_artifact_job_id`` of the predecessor whose durable bundle
    should be hydrated into the artifact dir before the command runs. The server
    resolves that predecessor id from the pinned job (the runner cannot ask for an
    arbitrary bundle), re-checks the predecessor belongs to the SAME project, and
    streams its ``bundle.tar.gz`` bytes.

    Returns 404 when the current job is not visible to this runner, carries no
    resume source, the predecessor is missing / cross-project, or its bundle
    object does not exist — fail-closed, never leaking whether an object exists.
    """
    allowed, retry_after = await check_runner_rate_limit(
        str(runner.id), "download_compute_job_input_artifact"
    )
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # Same ownership scoping as the report/upload endpoints: pinned runner only,
    # for any project on this runner's allow-list (multi-project runner).
    allowed_projects = await allowed_project_ids(session, runner)
    result = await session.execute(
        select(ComputeJob).where(
            ComputeJob.id == job_id,
            ComputeJob.project_id.in_(allowed_projects),
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            ComputeJob.polling_metadata["runner_target_id"].as_string()
            == str(runner.id),
        )
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")

    source_id_raw = (job.polling_metadata or {}).get("input_artifact_job_id")
    if not source_id_raw:
        raise HTTPException(status_code=404, detail="job has no resume source")
    try:
        source_id = uuid.UUID(str(source_id_raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="job has no resume source")

    # Defense-in-depth: the predecessor must still exist and belong to a project on
    # this runner's allow-list before we stream any bytes (launch-time validation
    # already enforced same-project, but re-check in case the row changed).
    source_job = await session.get(ComputeJob, source_id)
    if source_job is None or source_job.project_id != job.project_id:
        raise HTTPException(status_code=404, detail="resume source not found")

    from app import storage

    source_artifact = (source_job.polling_metadata or {}).get("runner_artifact")
    artifact_uri = source_artifact.get("uri") if isinstance(source_artifact, dict) else None
    if isinstance(artifact_uri, str) and storage.compute_job_id_for_artifact_uri(artifact_uri) != source_id:
        # A stamped URI must resolve back to the exact predecessor row.  Never
        # sign or proxy a malformed/cross-job reference, even within one project.
        raise HTTPException(status_code=404, detail="resume source bundle not found")
    if direct:
        if not isinstance(source_artifact, dict) or not source_artifact.get("stored"):
            raise HTTPException(status_code=404, detail="resume source bundle not found")
        if not isinstance(artifact_uri, str):
            raise HTTPException(status_code=404, detail="resume source bundle not found")
        try:
            download_url = await storage.generate_compute_job_artifact_uri_download_signed_url(
                artifact_uri
            )
        except Exception as exc:
            logger.warning("Direct artifact download authorization failed", exc_info=True)
            raise HTTPException(status_code=503, detail="resume source authorization failed") from exc
        return {
            "download_url": download_url,
            "expires_in_seconds": 3600,
            "size": source_artifact.get("size"),
            "crc32c": source_artifact.get("crc32c"),
            "generation": source_artifact.get("generation"),
        }

    if isinstance(artifact_uri, str):
        bundle = await storage.download_compute_job_artifact_uri(artifact_uri)
    else:
        # Older artifacts predate stamped exact-object URIs and use the fixed
        # per-job bundle path.  Keep that authenticated compatibility path.
        bundle = await storage.download_compute_job_artifact_bundle(source_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="resume source bundle not found")

    return Response(content=bundle, media_type="application/gzip")


@router.post("/compute-job/{job_id}/viewable")
async def upload_compute_job_viewable(
    job_id: uuid.UUID,
    request: Request,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Receive a single viewable file for a RUNNING compute job mid-execution.

    Runner-subprocess backends may push individual viewable files
    (png/jpg/jpeg/webp/gif/mp4/webm) WHILE the job is still RUNNING so they
    appear in the admin UI's ``viewable_artifacts`` list and strategic canvas
    BEFORE the job reaches a terminal state.

    Body is ``multipart/form-data``: a ``file`` field carrying the viewable file
    bytes, plus a ``filename`` form field with the relative path of the file
    (``reward_curve.png`` or ``arm1/reward.png`` — the same relpath the terminal
    ``persist_viewable_artifacts`` pipeline uses so the canonical storage URI
    matches and terminal explosion dedup works naturally).

    The endpoint enforces the same policies as the terminal viewable pipeline:
    extension-based kind classification, per-kind byte caps (8 MiB for images,
    512 MiB for videos), and the 25-file per-job count budget.  Terminal jobs are
    refused — mid-run uploads may only add evidence to a job that is still live.

    Scoped to the pinned runner exactly like ``/report`` and ``/artifact``, so
    runner A cannot upload viewable files to a job claimed by runner B.
    """
    allowed, retry_after = await check_runner_rate_limit(
        str(runner.id), "upload_compute_job_viewable"
    )
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    # ── Read the multipart body memory-safely (spool → streaming parse) ─────
    # Small bodies (images and small videos) stay in memory; a large video body
    # rolls to a temp file, so the orchestrator NEVER buffers a multi-hundred-MiB
    # payload on the heap (a per-request >1 GiB OOM vector under concurrent
    # pushes). The hard cap covers the largest viewable file (512 MiB video) plus
    # multipart framing overhead; the spool threshold is the image cap + margin.
    max_body_bytes = _MAX_VIEWABLE_VIDEO_BYTES + _MULTIPART_BODY_MARGIN
    spool_threshold = _MAX_VIEWABLE_FILE_BYTES + _MULTIPART_BODY_MARGIN
    spool = tempfile.SpooledTemporaryFile(max_size=spool_threshold)
    try:
        received = 0
        chunk_count = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body_bytes:
                raise HTTPException(status_code=413, detail="viewable file body exceeds upload cap")
            spool.write(chunk)
            chunk_count += 1
            # Yield the event loop every 64 chunks (~64 MiB at default chunk size)
            # so a large video upload cannot monopolize it during body ingestion.
            if chunk_count % 64 == 0:
                await asyncio.sleep(0)

        req_content_type = request.headers.get("content-type", "")

        # At/below the spool threshold the whole body is still in memory: reuse the
        # in-memory parser + upload seam (unchanged fast path for images and small
        # videos). Above it the body rolled to disk: parse and upload by STREAMING
        # the file part so its bytes never re-enter the heap.
        stream_upload = received > spool_threshold
        file_part: dict | None = None
        try:
            if stream_upload:
                # Run the synchronous disk-scan in a thread so the event loop
                # remains responsive while scanning a multi-hundred-MiB body.
                fields, file_part = await asyncio.to_thread(
                    _parse_multipart_form_stream, spool, req_content_type
                )
                if file_part is None:
                    raise HTTPException(
                        status_code=400, detail="missing 'file' field in multipart form"
                    )
                file_bytes = None
                file_length = file_part["length"]
            else:
                spool.seek(0)
                raw = spool.read()
                fields, files = _parse_multipart_form(raw, req_content_type)
                file_bytes = files.get("file")
                if file_bytes is None:
                    raise HTTPException(
                        status_code=400, detail="missing 'file' field in multipart form"
                    )
                file_length = len(file_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if file_length == 0:
            raise HTTPException(status_code=400, detail="empty viewable file body")

        raw_filename = (fields.get("filename") or b"").decode("latin-1", "replace").strip()
        if not raw_filename:
            raise HTTPException(status_code=400, detail="missing or empty 'filename' field")

        # New runners stamp one stable token per observed local-file generation
        # in a header so a response-loss retry does not fabricate another
        # revision. Legacy runners omit it; the server then creates a fresh token
        # for every accepted upload, preserving backward compatibility.
        raw_revision = request.headers.get("x-blueprint-viewable-revision")
        requested_revision = app_storage.normalize_viewable_revision(raw_revision)
        if raw_revision is not None and requested_revision is None:
            raise HTTPException(status_code=422, detail="invalid viewable revision")

        # ── Extension-based classification (reuses the terminal path's policy) ──
        type_info = app_storage.viewable_content_type_for_filename(raw_filename)
        if type_info is None:
            raise HTTPException(
                status_code=422,
                detail=f"file type not viewable: {raw_filename!r} — "
                f"supported: png/jpg/jpeg/webp/gif/mp4/webm/pdf/svg",
            )
        content_type, kind = type_info

        # ── Sanitize the relpath to match the terminal pipeline's object names ──
        safe_name = app_storage.sanitize_viewable_relpath(raw_filename)
        if safe_name is None:
            raise HTTPException(
                status_code=422,
                detail=f"filename {raw_filename!r} sanitizes to an empty or unsafe name",
            )

        # ── Enforce per-kind byte cap ───────────────────────────────────────────
        size_cap = _MAX_VIEWABLE_VIDEO_BYTES if kind == "video" else _MAX_VIEWABLE_FILE_BYTES
        if file_length > size_cap:
            raise HTTPException(
                status_code=413,
                detail=f"viewable file {safe_name!r} ({file_length} bytes) exceeds "
                f"the {size_cap}-byte cap for kind {kind!r}",
            )

        # ── Flat read: find the job, validate scope + pre-check caps ────────────
        allowed_projects = await allowed_project_ids(session, runner)
        _job_scope = (
            ComputeJob.id == job_id,
            ComputeJob.project_id.in_(allowed_projects),
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
        )
        result = await session.execute(select(ComputeJob).where(*_job_scope))
        job = result.scalars().first()
        if job is None:
            raise HTTPException(status_code=404, detail="compute job not found")

        if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="compute job is terminal — mid-run viewable uploads are refused",
            )

        # The storage_uri for this relpath is deterministic, so we can tell up front
        # whether this upload REFRESHES an already-registered file (an overwritten
        # reward_curve.png re-sent every heartbeat) versus ADDS a new one. A refresh
        # of an existing entry is exempt from the count cap — otherwise once the cap
        # is reached the runner keeps re-attempting a file it already sent and never
        # records success.
        expected_uri = app_storage.compute_job_viewable_uri(job_id, safe_name)

        def _is_registered(meta_dict: Any) -> bool:
            entries = (meta_dict or {}).get("viewable_files") or []
            if not isinstance(entries, list):
                return False
            return any(
                isinstance(e, dict) and e.get("storage_uri") == expected_uri
                for e in entries
            )

        # Pre-check: count cap under a NO-LOCK read (advisory — the locked re-read
        # below is authoritative).  If already at cap, refuse early to avoid the
        # slow GCS round-trip — but only for a genuinely NEW file; a refresh of an
        # already-registered relpath is always allowed through.
        meta_snapshot = job.polling_metadata or {}
        existing_snapshot = meta_snapshot.get("viewable_files") or []
        if (
            isinstance(existing_snapshot, list)
            and len(existing_snapshot) >= _MAX_VIEWABLE_FILE_COUNT
            and not _is_registered(meta_snapshot)
        ):
            raise HTTPException(
                status_code=429,
                detail=f"viewable file cap ({_MAX_VIEWABLE_FILE_COUNT}) reached for this job",
            )

        # ── Slow GCS upload to an unreferenced staging object (no row lock) ───
        # A rolled-to-disk video is STREAMED to GCS via a resumable upload (peak
        # memory bounded to the 8 MiB chunk window) rather than buffered whole;
        # in-memory files use the in-memory upload seam.  The canonical URI is
        # deterministic, so writing it here would let a slow live upload overwrite
        # the terminal bundle after terminal reconciliation won the row lock.
        # Promotion to that URI occurs only below, under the lock and after the
        # terminal re-check.
        staging_name = f".staging/{uuid.uuid4().hex}/{safe_name}"
        if stream_upload:
            uploaded_uri = await app_storage.upload_compute_job_viewable_stream(
                job_id,
                staging_name,
                _FileRangeReader(spool, file_part["start"], file_length),
                content_type,
                size=file_length,
            )
        else:
            uploaded_uri = await app_storage.upload_compute_job_viewable_file(
                job_id, staging_name, file_bytes, content_type,
            )
        if uploaded_uri is None:
            logger.warning(
                "[RUNNERS] GCS upload failed for viewable file %r on job %s — "
                "storage unavailable; completing request without it",
                safe_name,
                job_id,
            )
            raise HTTPException(status_code=502, detail="viewable file upload to storage failed")
    finally:
        # The spooled body (in-memory or the unlinked temp file) is fully consumed
        # once the upload above returns — release it before the DB section.
        spool.close()

    # ── Re-read with row lock: re-check terminal + count cap ───────────────
    result = await session.execute(
        select(ComputeJob)
        .where(*_job_scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job vanished mid-upload")

    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        logger.warning(
            "[RUNNERS] Compute job %s went terminal while viewable file %r "
            "upload was in flight — discarding its unreferenced staging object",
            job_id,
            safe_name,
        )
        await app_storage.delete_compute_job_viewable_file(job_id, staging_name)
        raise HTTPException(
            status_code=409,
            detail="compute job went terminal during upload — live artifact was discarded",
        )

    # The terminal transition cannot interleave this promotion: both paths hold
    # the job row lock.  Preserve the old uploader seam for deployments/tests
    # that return a canonical URI even when given a staging basename; production
    # storage returns the staging URI and therefore takes the promotion branch.
    expected_canonical_suffix = f"/artifacts/viewable/{safe_name}"
    canonical_uri = (
        uploaded_uri
        if uploaded_uri.endswith(expected_canonical_suffix)
        else await app_storage.promote_compute_job_viewable_file(
            job_id, staging_name, safe_name
        )
    )
    if canonical_uri is None:
        await app_storage.delete_compute_job_viewable_file(job_id, staging_name)
        raise HTTPException(status_code=502, detail="viewable file promotion to storage failed")
    uploaded_uri = canonical_uri

    meta = dict(job.polling_metadata or {})

    raw_existing_list = meta.get("viewable_files") or []
    if not isinstance(raw_existing_list, list):
        raw_existing_list = []
    # Detach nested JSON dicts from the ORM-owned polling_metadata value. A
    # SAVEPOINT rollback while repairing a missing signing row may expire/restore
    # the model attribute; mutating its shared list before that rollback would
    # make the response advertise a new revision while the DB retained the old.
    existing_list = [
        dict(item) if isinstance(item, dict) else item
        for item in raw_existing_list
    ]

    # Dedup by storage_uri: if this relpath was already uploaded (e.g. an
    # overwritten reward_curve.png), update the existing entry in place.
    existing_entry = None
    for manifest_entry in existing_list:
        if (
            isinstance(manifest_entry, dict)
            and manifest_entry.get("storage_uri") == uploaded_uri
        ):
            existing_entry = manifest_entry
            break

    # Count cap (definitive check under lock). A refresh of an ALREADY-registered
    # relpath updates its entry in place and does not grow the manifest, so it is
    # exempt from the cap — only a NEW file counts against it.
    if existing_entry is None and len(existing_list) >= _MAX_VIEWABLE_FILE_COUNT:
        # The staged GCS object we just promoted for this genuinely-new relpath has no
        # manifest entry and no ComputeJobFile row; the terminal pass reconciles
        # from the bundle tarball, not from stray viewable/ objects, so it would
        # be orphaned. Best-effort delete it before refusing so a lost cap race
        # (pre-lock advisory check passed, under-lock re-check trips) cannot leak
        # storage.
        await app_storage.delete_compute_job_viewable_file(job_id, safe_name)
        raise HTTPException(
            status_code=429,
            detail=f"viewable file cap ({_MAX_VIEWABLE_FILE_COUNT}) reached for this job",
        )

    # Reusing the runner's token makes transport retries idempotent. A legacy
    # upload (no header), or a genuinely changed runner generation, gets a new
    # identity even when storage_uri and byte size are unchanged.
    revision = requested_revision or app_storage.new_viewable_revision()

    entry: dict[str, Any] = {
        "filename": safe_name,
        "storage_uri": uploaded_uri,
        "content_type": content_type,
        "size_bytes": file_length,
        "kind": kind,
        "revision": revision,
        "live": True,
    }

    if existing_entry is not None:
        # Update in-place so a re-upload of the same relpath (overwritten
        # reward_curve.png) keeps one manifest entry.
        existing_entry.update(entry)
    else:
        existing_list.append(entry)
    meta["viewable_files"] = existing_list

    # ── Create/update ComputeJobFile signing row ───────────────────────────
    # Reconcile metadata in place for a deterministic URI. This avoids using an
    # expected UNIQUE violation as control flow on every live refresh and keeps
    # the row's size/content fields aligned with the latest stored generation.
    file_row = (
        await session.execute(
            select(ComputeJobFile).where(ComputeJobFile.storage_uri == uploaded_uri)
        )
    ).scalars().first()
    if file_row is not None:
        file_row.safe_relpath = safe_name
        file_row.media_type = kind
        file_row.content_type = content_type
        file_row.file_size_bytes = file_length
        session.add(file_row)
    else:
        try:
            async with session.begin_nested():
                cjf = ComputeJobFile(
                    compute_job_id=job.id,
                    safe_relpath=safe_name,
                    media_type=kind,
                    storage_uri=uploaded_uri,
                    content_type=content_type,
                    file_size_bytes=file_length,
                )
                session.add(cjf)
                await session.flush()
        except Exception:  # noqa: BLE001
            logger.warning(
                "[RUNNERS] ComputeJobFile insert failed for viewable file %r "
                "in job=%s — continuing without DB row (file is durable in GCS)",
                safe_name,
                job.id,
                exc_info=True,
            )

    # NOTE: We intentionally do NOT set ``viewable_file_count`` in
    # polling_metadata. That key is the idempotency guard for the terminal
    # ``persist_viewable_files_for_job`` pass; setting it here would skip the
    # terminal pass entirely, breaking reconciliation.  The terminal pass will
    # compute the authoritative superset manifest and stamp the count itself.

    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    return {
        "ok": True,
        "entry": entry,
        "total_viewable_files": len(existing_list),
    }
