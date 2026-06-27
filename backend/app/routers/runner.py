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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_agent_token, get_current_runner
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.database import get_session
from app.models import ComputeJob, ComputeJobStatus, LocalRunner, Project, Task, TaskStatus, _utcnow
from app.task_capabilities import agent_capability_slice

_MAX_RUNNER_COMMAND_LOG_BYTES = 65536
_MAX_RUNNER_RESULT_METRICS_KEYS = 256
# v1 cap on durable artifact uploads (mirrors _RUNNER_ARTIFACT_MAX_BYTES on the
# runner). The runner already gates on the uncompressed total; this caps the
# actual uploaded tarball as a server-side defense.
_MAX_RUNNER_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB
# Allowance for multipart framing/headers when early-rejecting on Content-Length.
_MULTIPART_BODY_MARGIN = 1 * 1024 * 1024  # 1 MiB

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


class HeartbeatRequest(BaseModel):
    active_task_ids: List[str] = []
    # Runner-reported hardware capabilities (optional; old runners omit these).
    has_gpu: Optional[bool] = None
    gpu_count: Optional[int] = None
    gpu_names: Optional[List[str]] = None


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

    logger.info(
        "[RUNNERS] Dispatched task %s (type=%s) to runner %s",
        task.id,
        task_type,
        runner.id,
    )
    return {
        "task_id": str(task.id),
        "agent_token": agent_token,
        "payload": task.payloads,
        "task_type": task_type,
        "capability_slice": agent_capability_slice(task_type),
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
    runner.last_seen_at = _utcnow()
    # Re-arm an inactive runner on every authenticated heartbeat. The stale-runner
    # sweep flips active->inactive after a single missed 300s window (slept/network-
    # dropped host); a successful heartbeat is proof of liveness, so flip it back to
    # active here. This is the reactivation half of allowing inactive runners to
    # authenticate (see auth.verify_runner_token). Never resurrects a deleted row:
    # a deleted runner can no longer authenticate, so it never reaches this handler.
    runner.status = "active"
    caps = dict(runner.capabilities or {})
    caps["active_task_ids"] = list(payload.active_task_ids)
    # Merge runner-reported hardware capabilities when present.
    # These override the registration-time default but never clobber
    # operator-configured fields (task_types, max_concurrent).
    if payload.has_gpu is not None:
        caps["has_gpu"] = payload.has_gpu
    if payload.gpu_count is not None:
        caps["gpu_count"] = payload.gpu_count
    if payload.gpu_names is not None:
        caps["gpu_names"] = payload.gpu_names
    runner.capabilities = caps
    session.add(runner)
    await session.commit()
    return {"ok": True, "last_seen_at": runner.last_seen_at.isoformat()}


class ComputeJobReportRequest(BaseModel):
    exec_status: str  # "running" | "succeeded" | "failed"
    exit_code: Optional[int] = None
    result_metrics: Optional[Dict[str, Any]] = None
    command_log: Optional[Dict[str, Any]] = None


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
