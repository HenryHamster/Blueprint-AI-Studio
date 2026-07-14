"""Agent-facing compute-job launch and polling endpoints."""

import base64
import binascii
import hmac
import json
import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.compute_backends import compute_job_heartbeat_secret
from app.compute_jobs import (
    ComputeLaunchPreconditionFailed,
    _COMPUTE_JOB_TERMINAL_STATUSES,
    append_progress_sample,
    create_compute_job,
    get_compute_job_for_task,
)
from app.database import get_session
from app.models import ComputeJob, ComputeJobStatus, Task
from app.routers.agent import verify_agent_token
from app.schemas import redact_command_log_text, serialize_compute_job_response
from app.shared_state import limiter
from app.task_capabilities import COMPUTE_JOB_LAUNCH_TASK_TYPES
from app.time_utils import _utcnow
from app.vm_cost_rates import resolve_compute_machine_type, supported_machine_types

_LIVE_TAIL_MAX_DECODED_BYTES = 20480
_LIVE_TAIL_MAX_TEXT_CHARS = 10240

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


class LaunchComputeJobRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = Field(..., min_length=1, max_length=10000)
    machine_type: str | None = Field(default=None, min_length=1, max_length=64)
    spend_cap_usd: float = Field(..., gt=0)
    deadline_seconds: int = Field(..., gt=0)
    cwd: str | None = Field(default=None, max_length=1000)
    env: Dict[str, str] | None = None
    backend_name: str | None = Field(default=None, max_length=32)
    provisioning_model: str | None = Field(default=None, max_length=16)
    # Optional single-GPU accelerator. Omission = CPU job (existing clients
    # unaffected); when set without a machine_type, create_compute_job resolves
    # the accelerator's canonical single-GPU shape and still rejects
    # A100/over-quota, count!=1, local backend, and explicit mismatches.
    accelerator: str | None = Field(default=None, max_length=32)
    # Optional marker for compute_experiment launches that claim to prove the
    # promoted/live system. Marked launches require aggregate release deploy
    # artifacts to be serving_confirmed before any job is created or reused.
    live_proof: bool | str | Dict[str, Any] | None = None
    live_proof_marker: str | None = Field(default=None, max_length=128)
    # Optional setup step executed as `bash -lc <setup_step>` AFTER the repo
    # clone and BEFORE the main command. On failure the job fails immediately
    # with reason `setup_step_failed`; no main-command budget is consumed.
    setup_step: str | None = Field(default=None, max_length=10000)
    # Optional no-progress watchdog window (peer of deadline_seconds). When set and
    # positive, a job that writes no intermediate progress (BLUEPRINT_PROGRESS_FILE)
    # within this many seconds is torn down early with reason `no_progress_timeout`.
    # Omission = inert watchdog; create_compute_job validates/clamps the value.
    no_progress_timeout_seconds: int | None = Field(default=None, gt=0)
    # Optional container image URI and registry auth. When container_image is set
    # the backend wraps the command in docker run. Only {server, username} are
    # accepted — password is rejected at create time (no delivery channel to the
    # backend launcher exists in the orchestrated path).
    container_image: str | None = Field(default=None, max_length=512)
    container_registry_auth: Dict[str, Any] | None = None
    # Optional cross-job checkpoint resume: UUID of a PREDECESSOR compute job whose
    # durable artifact bundle is rehydrated into this job's BLUEPRINT_ARTIFACT_DIR
    # before the command runs. create_compute_job validates that the predecessor
    # belongs to the same project and has a stored bundle. Omission = fresh workspace.
    input_artifact_job_id: str | None = Field(default=None, max_length=64)
    # Optional metric name the no-progress watchdog watches for a stall. When set,
    # a still-running job whose reported metric plateaus over the watchdog window
    # is cut early instead of burning its full deadline. Omission = no watchdog.
    declared_metric: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/compute-jobs")
@limiter.limit("30/minute")
async def launch_compute_job(
    request: Request,
    req: LaunchComputeJobRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    task_id_raw = token.get("sub")
    if not task_id_raw:
        raise HTTPException(status_code=403, detail="Invalid token: missing sub")
    try:
        task_id = uuid.UUID(task_id_raw)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid token: bad sub") from exc

    # CPU machine-type gate is unchanged. A GPU shape (accelerator present) is
    # NOT rejected here — it is accepted only when create_compute_job confirms it
    # is paired with a valid single-GPU accelerator (which also rejects
    # A100/over-quota, count!=1, the local backend, and family mismatches).
    resolved_machine_type = resolve_compute_machine_type(
        accelerator=req.accelerator,
        machine_type=req.machine_type,
    )
    if (
        req.accelerator is None
        and resolved_machine_type not in supported_machine_types()
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_machine_type",
                "supported_machine_types": list(supported_machine_types()),
            },
        )

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task_type = str(task.first_payload.get("type") or "")
    if task_type not in COMPUTE_JOB_LAUNCH_TASK_TYPES:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "task_type_not_allowed_for_compute_jobs",
                "task_type": task_type,
                "allowed_task_types": sorted(COMPUTE_JOB_LAUNCH_TASK_TYPES),
            },
        )

    try:
        job = await create_compute_job(
            session,
            task,
            command=req.command,
            machine_type=req.machine_type,
            spend_cap_usd=req.spend_cap_usd,
            deadline_seconds=req.deadline_seconds,
            cwd=req.cwd,
            env=req.env,
            backend_name=req.backend_name,
            provisioning_model=req.provisioning_model,
            accelerator=req.accelerator,
            live_proof=req.live_proof,
            live_proof_marker=req.live_proof_marker,
            setup_step=req.setup_step,
            no_progress_timeout_seconds=req.no_progress_timeout_seconds,
            container_image=req.container_image,
            container_registry_auth=req.container_registry_auth,
            declared_metric=req.declared_metric,
            input_artifact_job_id=req.input_artifact_job_id,
        )
    except ComputeLaunchPreconditionFailed as exc:
        raise HTTPException(status_code=412, detail=exc.detail) from exc
    except ValueError as exc:
        logger.warning(
            "Validation error launching compute job: %s: %s", type(exc).__name__, exc
        )
        raise HTTPException(status_code=422, detail="Invalid request") from exc

    return {"status": "success", "data": serialize_compute_job_response(job)}


@router.get("/compute-jobs/{job_id}")
@limiter.limit("120/minute")
async def poll_compute_job(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    task_id_raw = token.get("sub")
    if not task_id_raw:
        raise HTTPException(status_code=403, detail="Invalid token: missing sub")
    try:
        task_id = uuid.UUID(task_id_raw)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid token: bad sub") from exc

    job = await get_compute_job_for_task(session, job_id=job_id, task_id=task_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")
    return {"status": "success", "data": serialize_compute_job_response(job)}


@router.post("/compute-jobs/{job_id}/heartbeat", status_code=204)
@limiter.limit("120/minute")
async def compute_job_vm_heartbeat(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Liveness push from a GCP compute-job VM.

    The VM's startup script POSTs here every ~60 s so the orchestrator can confirm
    the VM is alive without relying on serial-console polls or the GCP insert
    long-running-operation.  The first heartbeat also transitions the job from
    STARTING to RUNNING so provisioning success is declared on boot rather than on
    LRO completion, and the body may carry progress samples that feed the
    no-progress watchdog.

    Authentication: the bearer token is NOT the job UUID.  The UUID is a resource
    identifier — it is in the URL path, agent poll responses, and log/serial
    output — so authenticating with it would let any observer suppress the
    no-progress watchdog or keep a stuck STARTING job alive.  Instead the VM sends
    a per-job secret derived by ``compute_job_heartbeat_secret`` (HMAC of the job
    id keyed by ``AGENT_JWT_SECRET``), embedded only in the encrypted startup
    script and never echoed by any API.  The endpoint recomputes it and does a
    constant-time compare; a bare-UUID caller is rejected with 403.  The endpoint
    is also rate-limited and only mutates active (non-terminal) jobs.
    """
    auth = request.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip()
    expected = compute_job_heartbeat_secret(job_id)
    # Compare as bytes (constant-time): hmac.compare_digest raises on a str with
    # non-ASCII chars, so a hostile Authorization header must not 500 the endpoint.
    if not hmac.compare_digest(bearer.encode("utf-8"), expected.encode("ascii")):
        raise HTTPException(status_code=403, detail="Invalid compute job token")

    job = await session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")

    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        # VM doesn't need to know it has been terminated — just return 204 so
        # it can finish its cleanup without retrying on an error response.
        return Response(status_code=204)

    now = _utcnow()
    job.last_heartbeat_at = now
    job.updated_at = now
    # First heartbeat: the VM has booted — transition from STARTING to RUNNING
    # so the provisioning timeout no longer applies and cost accrual begins.
    if job.status == ComputeJobStatus.STARTING:
        job.status = ComputeJobStatus.RUNNING
        if job.started_at is None:
            job.started_at = now
    # Optional live output tail: the VM runner writes a base64 JSON tail every
    # ~30 s and includes it in the heartbeat body. Parse and store it so the
    # operator can see partial output while the job runs. All failures degrade
    # gracefully — a malformed or missing body never causes a non-204 response.
    body_bytes = await request.body()
    live_output = _parse_live_output_tail(body_bytes)
    if live_output is not None:
        meta = dict(job.polling_metadata or {})
        meta["live_output"] = live_output
        job.polling_metadata = meta
    # Optional progress sample: the runner may include {"progress": {"metric": ...,
    # "value": ...}} in the heartbeat body. Malformed or missing fields are silently
    # ignored — older runners that never send this keep working byte-for-byte.
    progress_sample = _parse_progress_sample(body_bytes)
    if progress_sample is not None:
        metric, value = progress_sample
        job.polling_metadata = append_progress_sample(
            dict(job.polling_metadata or {}),
            metric=metric,
            value=value,
            t=_utcnow().isoformat(),
        )
    await session.commit()
    return Response(status_code=204)


def _parse_live_output_tail(body: bytes) -> dict[str, Any] | None:
    """Decode a heartbeat body and extract a safe live_output dict, or None."""
    if not body:
        return None
    try:
        body_json = json.loads(body)
    except (ValueError, TypeError):
        return None
    raw_tail = body_json.get("live_output_tail") if isinstance(body_json, dict) else None
    if not isinstance(raw_tail, str) or not raw_tail:
        return None
    try:
        decoded = base64.b64decode(raw_tail.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if len(decoded) > _LIVE_TAIL_MAX_DECODED_BYTES:
        return None
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    text = parsed.get("text")
    truncated = parsed.get("truncated")
    if not isinstance(text, str) or not isinstance(truncated, bool):
        return None
    text = redact_command_log_text(text[: _LIVE_TAIL_MAX_TEXT_CHARS])
    return {"text": text, "truncated": truncated}


def _parse_progress_sample(body: bytes) -> tuple[str, float] | None:
    """Extract a (metric, value) progress sample from a heartbeat body, or None.

    Accepts ``{"progress": {"metric": <str>, "value": <number>}}`` at the top
    level of the JSON body.  Any missing, non-string, or non-numeric field
    returns None so callers degrade gracefully.
    """
    if not body:
        return None
    try:
        body_json = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(body_json, dict):
        return None
    progress = body_json.get("progress")
    if not isinstance(progress, dict):
        return None
    metric = progress.get("metric")
    if not isinstance(metric, str) or not metric:
        return None
    raw_value = progress.get("value")
    try:
        float_val = float(raw_value)
    except (TypeError, ValueError):
        return None
    return metric, float_val
