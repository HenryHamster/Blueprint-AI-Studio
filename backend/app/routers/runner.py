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
    (running|succeeded|failed), exit_code, result_metrics, command_log. The
    monitor's existing poll_status/finalize path picks up the stamped terminal
    status. Returns {ok, cancel_requested}.

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
from pydantic import BaseModel, StrictBool, StrictStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_agent_token, get_current_runner
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.compute_manifest import _apply_llm_env, _resolve_provider_materials
from app.runner_rate_limiter import check_runner_rate_limit
from app.database import get_session
from app.models import ComputeJob, ComputeJobStatus, LocalRunner, Project, Task, TaskStatus, _utcnow
from app.shared_state import runtime_config as _runtime_config
from app.task_capabilities import agent_capability_slice
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
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
_RUNNER_LIVENESS_SECONDS = 300

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


class HeartbeatRequest(BaseModel):
    active_task_ids: List[str] = []
    active_compute_job_ids: List[str] = []
    agent_available: StrictBool | None = None
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
    # Runner-reported hardware capabilities (optional; old runners omit these).
    has_gpu: StrictBool | None = None
    gpu_count: int | None = None
    gpu_names: List[str] | None = None
    # Trust telemetry (optional; old runners omit these). last_successful_job_at
    # is ISO-8601; consecutive_job_failures/disk_free_bytes/gpu_utilization_pct are
    # ints. These drive the "connected but failing" degraded state so the operator
    # console can tell a healthy runner from one heartbeating while every job fails.
    last_successful_job_at: str | None = None
    consecutive_job_failures: int | None = None
    disk_free_bytes: int | None = None
    gpu_utilization_pct: int | None = None


@router.post("/next-task")
async def next_task(
    response: Response,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Return the oldest task pinned to this runner, or 204 if none.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent polls from the same
    runner never double-claim a task. On a hit a task-scoped agent JWT is minted
    and returned; the task stays in PROVISIONING so the agent binary can call
    ``/api/agent/claim`` to perform the PROVISIONING → IN_PROGRESS transition
    (identical contract to GCP-provisioned VMs).
    """
    allowed, retry_after = await check_runner_rate_limit(str(runner.id), "next_task")
    if not allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})

    pinned_vm_id = f"runner:{runner.id}"
    result = await session.execute(
        select(Task)
        .where(
            Task.status == TaskStatus.PROVISIONING,
            Task.vm_instance_id == pinned_vm_id,
            Task.context["runner_target_id"].as_string() == str(runner.id),
            # Exclude tasks already dispatched: started_at is set on dispatch so
            # a re-poll while the agent is booting and calling /api/agent/claim
            # skips this task instead of returning it again (dispatch-storm guard).
            Task.started_at.is_(None),
        )
        .order_by(Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = result.scalars().first()
    if task is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    # Mark as dispatched so re-polls skip this task while the agent boots and
    # calls /api/agent/claim.  The task stays in PROVISIONING so claim can still
    # perform the PROVISIONING → IN_PROGRESS transition (same contract as GCP VMs).
    task.started_at = _utcnow()
    session.add(task)
    await session.commit()

    task_type = (task.first_payload or {}).get("type", "")
    agent_token = create_agent_token(
        str(task.id),
        vm_instance_id=pinned_vm_id,
        task_type=task_type,
        project_id=str(task.project_id),
    )

    # Resolve a pooled LLM account for this task — same path as the orchestrator's
    # cloud-VM dispatch (select_llm_runtime_for_task).  The self-hosted runner no
    # longer carries its own static LLM credentials; the backend assigns one per
    # task from the pool, and the runner injects the resolved credential env vars
    # into the agent process.
    llm_env: dict[str, str] = {}
    llm_files: list[dict[str, str]] = []
    llm_provider: str | None = None
    llm_auth_mode: str | None = None
    llm_model: str | None = None

    try:
        from app.max_plan_pool import select_llm_runtime_for_task

        # Resolve the billing user: launcher pays; fall back to project owner.
        if task.launched_by_user_id:
            dispatch_user_id = task.launched_by_user_id
        else:
            task_project = await session.get(Project, task.project_id)
            dispatch_user_id = task_project.user_id if task_project else ""

        runtime_selection = await select_llm_runtime_for_task(
            user_id=dispatch_user_id,
            task=task,
            runtime_config=_runtime_config,
        )
        if runtime_selection is None or runtime_selection.account is None:
            # Pool exhausted — no eligible account.  Roll back the dispatch marker
            # so the task stays claimable by a future poll once an account frees up.
            logger.warning(
                "[RUNNERS] No LLM account available for task %s (type=%s) — "
                "pool exhausted; leaving task unclaimed",
                task.id,
                task_type,
            )
            task.started_at = None
            session.add(task)
            await session.commit()
            response.status_code = status.HTTP_204_NO_CONTENT
            return response

        acct = runtime_selection.account
        llm_provider = runtime_selection.provider
        llm_auth_mode = runtime_selection.auth_mode
        llm_model = runtime_selection.model

        # Build the credential env vars and files from the resolved account using
        # the SAME material-resolution path as cloud-VM boot and warm re-target.
        runtime_secret = acct.get("runtime_secret") or acct.get("oauth_token")
        materials = _resolve_provider_materials(
            oauth_token=runtime_secret,
            claude_config_json=acct.get("claude_config_json"),
            provider=llm_provider,
            auth_mode=llm_auth_mode,
            model=llm_model,
            provider_metadata=acct.get("provider_metadata") or {},
            provider_settings=runtime_selection.settings,
        )
        llm_env = {
            "PROVIDER": materials.provider_name,
            "AUTH_MODE": materials.auth_mode_value,
        }
        _apply_llm_env(
            llm_env,
            model=llm_model,
            execution_mode=None,
            provider_settings=runtime_selection.settings,
            materials=materials,
        )
        llm_files = [
            {"path": f.path, "content": f.content, "mode": str(f.mode)}
            for f in materials.files
        ]

        # Persist the resolved account identity in the task context so the
        # completion / failure handler can release the account slot and record
        # the actual provider/model used for analytics.
        task.context = {
            **(task.context or {}),
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "llm_auth_mode": llm_auth_mode,
            "llm_account_id": acct.get("id"),
        }
        # Keep the promoted column in sync with context (D2 over-fetch fix).
        task.llm_account_id = acct.get("id")
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(task, "context")
        session.add(task)
        await session.commit()

        logger.info(
            "[RUNNERS] Dispatched task %s (type=%s) to runner %s "
            "with pooled account %s (provider=%s model=%s)",
            task.id,
            task_type,
            runner.id,
            acct.get("id"),
            llm_provider,
            llm_model,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "[RUNNERS] LLM account selection failed for task %s — "
            "rolling back dispatch",
            task.id,
        )
        task.started_at = None
        session.add(task)
        await session.commit()
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return {
        "task_id": str(task.id),
        "agent_token": agent_token,
        "vm_instance_id": pinned_vm_id,
        "payload": task.payloads,
        "task_type": task_type,
        "capability_slice": agent_capability_slice(task_type),
        "provider": llm_provider,
        "auth_mode": llm_auth_mode,
        "model": llm_model,
        "llm_env": llm_env,
        "llm_files": llm_files,
    }


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
    runner.status = "active"
    caps = dict(runner.capabilities or {})
    caps["active_task_ids"] = list(payload.active_task_ids)
    caps["active_compute_job_ids"] = list(payload.active_compute_job_ids)
    # Merge runner-reported hardware capabilities when present.
    # These override the registration-time default but never clobber
    # operator-configured fields (task_types, max_concurrent).
    if payload.agent_available is not None:
        caps["agent_available"] = payload.agent_available
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
    if payload.gpu_utilization_pct is not None:
        caps["gpu_utilization_pct"] = max(0, min(100, int(payload.gpu_utilization_pct)))
    if payload.hostname:
        runner.hostname = payload.hostname[:255]
    if payload.version:
        runner.version = payload.version[:64]
    runner.capabilities = caps
    session.add(runner)
    await session.commit()
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

    return {
        "ok": True,
        "last_seen_at": runner.last_seen_at.isoformat(),
        "enrolled_max_concurrent": caps.get("max_concurrent"),
        "version_hint": _current_sha,
    }


class ComputeJobReportRequest(BaseModel):
    exec_status: str  # "running" | "succeeded" | "failed"
    exit_code: int | None = None
    result_metrics: Dict[str, Any] | None = None
    command_log: Dict[str, Any] | None = None


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
        raise HTTPException(status_code=404, detail="compute job not found")

    _TERMINAL_STATUSES = {
        ComputeJobStatus.SUCCEEDED,
        ComputeJobStatus.FAILED,
        ComputeJobStatus.CANCELLED,
        ComputeJobStatus.TIMED_OUT,
    }

    meta = dict(job.polling_metadata or {})
    cancel_requested = bool(job.status in _TERMINAL_STATUSES)

    if job.status not in _TERMINAL_STATUSES:
        valid_statuses = {"running", "succeeded", "failed"}
        if payload.exec_status in valid_statuses:
            meta["runner_exec_status"] = payload.exec_status
        if payload.exit_code is not None and isinstance(payload.exit_code, int):
            meta["runner_exit_code"] = payload.exit_code
        if payload.result_metrics is not None and isinstance(payload.result_metrics, dict):
            meta["runner_result_metrics"] = _filter_result_metrics(payload.result_metrics)
        if payload.command_log is not None:
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
        if payload.exec_status in ("succeeded", "failed"):
            meta["runner_finished_at"] = now.isoformat()
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

    raw = await request.body()
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
    result = await session.execute(
        select(ComputeJob)
        .where(
            ComputeJob.id == job_id,
            ComputeJob.project_id == runner.project_id,
            ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
            ComputeJob.polling_metadata["runner_target_id"].as_string() == str(runner.id),
        )
        .with_for_update()
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="compute job not found")

    # store_runner_artifact is implemented in parallel in app.compute_backends
    # (pinned contract). Imported locally — same pattern as the repo-token import
    # in /next-compute-job — so module import / app boot never depends on its
    # presence at import time.
    from app.compute_backends import store_runner_artifact

    artifact_info = await store_runner_artifact(
        session, job, archive_bytes, file_count, total_bytes
    )

    meta = dict(job.polling_metadata or {})
    meta["runner_artifact"] = artifact_info
    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    return {"ok": True, "runner_artifact": artifact_info}
