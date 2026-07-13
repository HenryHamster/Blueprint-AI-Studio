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

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool, StrictStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.auth import create_agent_token, get_current_runner
from app.broadcast import broadcast_to_project
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.runner_rate_limiter import check_runner_rate_limit
from app.runner_config import config_generation, live_safe_config
from app.runner_pull_skip import (
    SKIP_CODE_CREDENTIAL_ERROR,
    SKIP_CODE_NO_ACCOUNT,
    cached_skip,
    clear_skip_marker,
    has_skip_marker,
    maybe_escalate_pull_skip,
    skip_marker_is_fresh,
    skip_seen_at,
    stamp_skip_reason,
)
from app.database import get_session
from app.models import Alert, AlertSeverity, ComputeJob, ComputeJobStatus, LocalRunner, Project, RunnerHeartbeatSnapshot, Task, TaskStatus, _utcnow
from app.runner_decline import (
    DECLINE_ESCALATION_THRESHOLD as _DECLINE_ESCALATION_THRESHOLD,
    DECLINED_BY_CONTEXT_KEY,
    maybe_create_decline_alert as _maybe_create_decline_alert,
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

_MAX_RUNNER_COMMAND_LOG_BYTES = 65536
_MAX_RUNNER_RESULT_METRICS_KEYS = 256
# v1 cap on durable artifact uploads (mirrors _RUNNER_ARTIFACT_MAX_BYTES on the
# runner). The runner already gates on the uncompressed total; this caps the
# actual uploaded tarball as a server-side defense.
_MAX_RUNNER_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB
# Allowance for multipart framing/headers when early-rejecting on Content-Length.
_MULTIPART_BODY_MARGIN = 1 * 1024 * 1024  # 1 MiB
# Interim live-output tail (running reports only). Stamped as
# polling_metadata["live_output"] — the same key/shape the GCP live-tail path
# writes (compute_jobs._apply_status_update + agent_compute_jobs heartbeat) and
# the admin UI's live-output block reads. The runner sends ~4 KiB; server cap.
_MAX_RUNNER_LIVE_OUTPUT_BYTES = 8192
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
    - strings, None, non-finite floats, nested objects → dropped
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
        # strings, None, nested dicts/lists → dropped
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


def _merge_progress_report(existing: Any, raw: Any, now: datetime) -> dict | None:
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
        return None
    raw_records = raw.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        return None
    if len(raw_records) > _MAX_PROGRESS_RECORDS_PER_REPORT * 5:
        # Wildly oversized payload: reject wholesale rather than paying to
        # validate an unbounded list (the runner never sends more than 20).
        return None
    records = [
        rec
        for rec in (
            _filter_progress_record(item)
            for item in raw_records[-_MAX_PROGRESS_RECORDS_PER_REPORT:]
        )
        if rec is not None
    ]
    if not records:
        return None

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
        return None
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
        return None
    merged = (prev_records + new_records)[-_MAX_PROGRESS_RECORDS_STORED:]
    return {
        "records": merged,
        "count": max(count, len(merged)),
        "updated_at": now.isoformat(),
    }


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
    # Runner-reported isolation-backend inventory + selected backends (optional;
    # old runners omit these). isolation_backends is a flat {backend: bool} map
    # (bwrap_ok/userns_ok/docker_usable/podman_usable/apptainer_ok/enroot_ok/
    # systemd_run_ok); agent_backend/compute_backend name the backend the runner
    # would use for each class. All ride in capabilities JSONB for the fleet panel.
    isolation_backends: dict | None = None
    agent_backend: str | None = None
    compute_backend: str | None = None
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
    disk_free_bytes: int | None = None
    cpu_load_avg: float | None = None
    memory_free_bytes: int | None = None
    gpu_utilization_pct: int | None = None
    gpu_free_vram_mib: List[int] | None = None
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

        from app.max_plan_pool import select_llm_runtime_for_task
        from app.compute_manifest import (
            _apply_llm_env,
            _resolve_provider_materials,
        )

        # Resolve the billing user: launcher pays; fall back to project owner.
        dispatch_user_id = (
            task.launched_by_user_id
            if task.launched_by_user_id
            else project.user_id
        )

        failure_reasons: list[str] = []
        selection = await select_llm_runtime_for_task(
            user_id=dispatch_user_id,
            task=task,
            runtime_config=_rc_dict,
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

        # Build credential env/files using the same provider-impl path that
        # cloud-VM manifests use — BEFORE stamping the task, so an exception
        # here leaves the row untouched.
        materials = _resolve_provider_materials(
            oauth_token=account.get("runtime_secret") or account.get("oauth_token"),
            claude_config_json=account.get("claude_config_json"),
            provider=selection.provider,
            auth_mode=selection.auth_mode,
            model=selection.model,
            provider_metadata=account.get("provider_metadata"),
            provider_settings=selection.settings,
        )
        cred_env: dict[str, str] = {
            "PROVIDER": materials.provider_name,
            "AUTH_MODE": materials.auth_mode_value,
        }
        _apply_llm_env(
            cred_env,
            model=selection.model,
            execution_mode=selection.execution_mode,
            provider_settings=selection.settings,
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
            selection.provider,
            selection.model,
        )
        # The runtime-context stamp happens in _apply_dispatch_credentials, on
        # the winner only — see this function's docstring.
        return (
            True,
            _ResolvedDispatch(
                credentials=credentials,
                account_id=account.get("id"),
                provider=selection.provider,
                auth_mode=selection.auth_mode,
                model=selection.model,
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

    pinned_vm_id = f"runner:{runner.id}"
    # Skip markers are scoped to the runner that wrote them: routability depends on
    # the runner's own CLI inventory, so another runner's verdict says nothing about
    # what THIS one can run (a task unpinned from A and re-pinned to B must not
    # inherit A's "unroutable" and be refused by B, which could run it fine).
    runner_id = str(runner.id)
    claim_guard = (
        Task.status == TaskStatus.PROVISIONING,
        Task.project_id == runner.project_id,
        Task.vm_instance_id == pinned_vm_id,
        Task.context["runner_target_id"].as_string() == str(runner.id),
        # Exclude tasks already dispatched: started_at is set on dispatch so
        # a re-poll while the agent is booting and calling /api/agent/claim
        # skips this task instead of returning it again (dispatch-storm guard).
        Task.started_at.is_(None),
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
    lost_races = 0
    attempts_left = _next_task_resolve_attempts()
    now = _utcnow()

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
        "agent_token": agent_token,
        "vm_instance_id": pinned_vm_id,
        "payload": task.payloads,
        "task_type": task_type,
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

    # ── Record the decline so the dispatcher can stop re-picking this runner ──
    declined_by = dict(new_ctx.get(DECLINED_BY_CONTEXT_KEY) or {})
    prev = declined_by.get(str(runner.id)) or {}
    count = int(prev.get("count") or 0) + 1
    declined_by[str(runner.id)] = {
        "count": count,
        "at": _utcnow().isoformat(),
        "reason": reason[:500],
        "hostname": runner.hostname,
    }
    new_ctx[DECLINED_BY_CONTEXT_KEY] = declined_by
    total_declines = sum(
        int((rec or {}).get("count") or 0) for rec in declined_by.values()
    )
    task.context = new_ctx
    flag_modified(task, "context")
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
        caps["active_work_reported_at"] = now.isoformat()
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
    if payload.llm_clis is not None:
        caps["llm_clis"] = payload.llm_clis
    if payload.agent_backend is not None:
        caps["agent_backend"] = payload.agent_backend
    if payload.compute_backend is not None:
        caps["compute_backend"] = payload.compute_backend
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
    if payload.cpu_load_avg is not None:
        caps["cpu_load_avg"] = max(0.0, float(payload.cpu_load_avg))
    if payload.memory_free_bytes is not None:
        caps["memory_free_bytes"] = max(0, int(payload.memory_free_bytes))
    if payload.gpu_utilization_pct is not None:
        caps["gpu_utilization_pct"] = max(0, min(100, int(payload.gpu_utilization_pct)))
    if payload.gpu_free_vram_mib is not None:
        caps["gpu_free_vram_mib"] = [max(0, int(v)) for v in payload.gpu_free_vram_mib]
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
            caps["version_changed_at"] = now.isoformat()
            caps["previous_version"] = previous_version
        runner.version = new_version
    runner.capabilities = caps
    session.add(runner)
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
        "last_seen_at": runner.last_seen_at.isoformat(),
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
    """Create an operator alert for a runner's self-update outcome.

    Called best-effort by ``blueprint-runner update`` after the update
    completes (or fails).  Creates an INFO alert on success, WARNING on
    failure, and broadcasts it so the operator console surfaces the outcome
    immediately — no need to SSH or grep logs to see if the version bump
    landed.

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

    alert = Alert(
        project_id=runner.project_id,
        severity=severity,
        title=title,
        message=message[:1000],
        source="runner",
        event_type="runner.self_update",
    )
    session.add(alert)
    await session.commit()

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

    now = _utcnow()
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.project_id == runner.project_id,
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

    job.status = ComputeJobStatus.RUNNING
    job.started_at = job.started_at or now
    job.last_heartbeat_at = now
    job.updated_at = now
    meta = dict(job.polling_metadata or {})
    meta["runner_claimed_at"] = now.isoformat()
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

            project = await session.get(Project, runner.project_id)
            if project is not None:
                repo_token = await _get_project_github_token(project, session)
        except Exception:
            logger.warning(
                "[RUNNERS] Failed to mint repo token for compute job %s; "
                "runner will execute without a repo checkout",
                job.id,
            )

    return {
        "job_id": str(job.id),
        "command": refreshed_meta.get("command", ""),
        "env": refreshed_meta.get("env") or {},
        "cwd": refreshed_meta.get("cwd"),
        "setup_step": refreshed_meta.get("setup_step"),
        "container_image": refreshed_meta.get("container_image"),
        "container_registry_auth": refreshed_meta.get("container_registry_auth"),
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
    }


@router.post("/compute-job/{job_id}/report")
async def report_compute_job(
    job_id: uuid.UUID,
    payload: ComputeJobReportRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Runner reports execution status for a claimed compute job.

    Stamps exec_status, exit_code, result_metrics, and command_log into
    ``polling_metadata`` so the monitor's existing ``poll_status``/finalize path
    can pick up the terminal state. Does NOT finalize the job itself — the monitor
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
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.id == job_id,
            ComputeJob.project_id == runner.project_id,
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            # Scope to the pinned runner — mirrors the claim filter in
            # /next-compute-job so runner A cannot report on a job claimed by B.
            ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
        )
        .with_for_update()
    )
    job = result.scalars().first()
    if job is None:
        # Check whether the job exists under a different project — if so, emit a
        # structured warning so operators can detect misconfigured runners.
        mismatch_result = await session.execute(
            select(ComputeJob).where(ComputeJob.id == job_id).limit(1)
        )
        mismatch_job = mismatch_result.scalars().first()
        if mismatch_job is not None:
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

    meta = dict(job.polling_metadata or {})
    cancel_requested = bool(job.status in _COMPUTE_JOB_TERMINAL_STATUSES)

    if job.status not in _COMPUTE_JOB_TERMINAL_STATUSES:
        valid_statuses = {"running", "succeeded", "failed"}
        if payload.exec_status in valid_statuses:
            meta["runner_exec_status"] = payload.exec_status
        if payload.exit_code is not None and isinstance(payload.exit_code, int):
            meta["runner_exit_code"] = payload.exit_code
        if payload.result_metrics is not None and isinstance(payload.result_metrics, dict):
            meta["runner_result_metrics"] = _filter_result_metrics(payload.result_metrics)
        if payload.command_log is not None and payload.exec_status == "running":
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
            if isinstance(payload.command_log, dict):
                log_text = payload.command_log.get("text", "")
                if not isinstance(log_text, str):
                    log_text = ""
                if len(log_text.encode("utf-8")) > _MAX_RUNNER_COMMAND_LOG_BYTES:
                    log_text = log_text.encode("utf-8")[:_MAX_RUNNER_COMMAND_LOG_BYTES].decode("utf-8", "replace")
                    meta["runner_command_log"] = {"text": log_text, "truncated": True}
                else:
                    meta["runner_command_log"] = {"text": log_text, "truncated": False}
            elif isinstance(payload.command_log, str):
                meta["runner_command_log"] = {"text": payload.command_log, "truncated": False}
        if payload.progress is not None:
            merged_progress = _merge_progress_report(
                meta.get("progress"), payload.progress, now
            )
            if merged_progress is not None:
                meta["progress"] = merged_progress
        if payload.exec_status in ("succeeded", "failed"):
            meta["runner_finished_at"] = now.isoformat()
        if isinstance(payload.resume_hydration, dict):
            # Whitelist known provenance keys so a runner cannot bloat the row.
            raw = payload.resume_hydration
            provenance: Dict[str, Any] = {}
            src = raw.get("resumed_from_job_id")
            if isinstance(src, str) and src.strip():
                provenance["resumed_from_job_id"] = src.strip()[:64]
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
                provenance["recorded_at"] = now.isoformat()
                meta["resume_provenance"] = provenance
        job.polling_metadata = meta
        job.last_heartbeat_at = now
        job.updated_at = now
        session.add(job)
        await session.commit()

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
            fields[name] = content
    return fields, files


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
    _job_scope = (
        ComputeJob.id == job_id,
        ComputeJob.project_id == runner.project_id,
        ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
        ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
    )
    result = await session.execute(select(ComputeJob).where(*_job_scope))
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")

    # store_runner_artifact is implemented in parallel in app.compute_backends
    # (pinned contract). Imported locally — same pattern as the repo-token import
    # in /next-compute-job — so module import / app boot never depends on its
    # presence at import time.
    from app.compute_backends import store_runner_artifact

    # Slow storage upload with NO row lock held (uses only job.id).
    artifact_info = await store_runner_artifact(
        session, job, archive_bytes, file_count, total_bytes
    )

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
    meta["runner_artifact"] = artifact_info
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    return {"ok": True, "runner_artifact": artifact_info}


@router.get("/compute-job/{job_id}/input-artifact")
async def download_compute_job_input_artifact(
    job_id: uuid.UUID,
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

    # Same ownership scoping as the report/upload endpoints: pinned runner only.
    result = await session.execute(
        select(ComputeJob).where(
            ComputeJob.id == job_id,
            ComputeJob.project_id == runner.project_id,
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

    # Defense-in-depth: the predecessor must still exist and belong to this
    # runner's project before we stream any bytes (launch-time validation already
    # enforced same-project, but re-check in case the row changed).
    source_job = await session.get(ComputeJob, source_id)
    if source_job is None or source_job.project_id != runner.project_id:
        raise HTTPException(status_code=404, detail="resume source not found")

    from app import storage

    bundle = await storage.download_compute_job_artifact_bundle(source_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="resume source bundle not found")

    return Response(content=bundle, media_type="application/gzip")
