"""Agent-facing compute-job launch and polling endpoints."""

import base64
import binascii
import hmac
import json
import logging
import tempfile
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.compute_backends import (
    _MAX_VIEWABLE_FILE_BYTES,
    _MAX_VIEWABLE_FILE_COUNT,
    _MAX_VIEWABLE_VIDEO_BYTES,
    compute_job_heartbeat_secret,
)
from app.compute_jobs import (
    ComputeLaunchPreconditionFailed,
    _COMPUTE_JOB_TERMINAL_STATUSES,
    append_progress_sample,
    create_compute_job,
    get_compute_job_for_task,
)
from app.database import get_session
from app.models import ComputeJob, ComputeJobFile, ComputeJobStatus, Task
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


def _accepts_legacy_compute_heartbeat(job: ComputeJob, bearer: str) -> bool:
    """Whether a pre-HMAC VM may use its legacy UUID bearer.

    A job is treated as launched under the HMAC contract when its
    polling_metadata carries ``heartbeat_auth == "hmac"`` — a marker stamped at
    launch time by the backend that embedded the HMAC token in the startup
    script.  Jobs without that marker were launched by a pre-HMAC engine and
    may authenticate with the bare job UUID (constant-time compare).  Jobs with
    the marker are HMAC-only (fail-closed).
    """
    meta = job.polling_metadata or {}
    if meta.get("heartbeat_auth") == "hmac":
        return False
    return hmac.compare_digest(bearer.encode("utf-8"), str(job.id).encode("ascii"))


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
    constant-time compare. During the flag-day rollout only, pre-rollout jobs
    accept their legacy UUID bearer so already-running VMs cannot be reaped
    mid-job; jobs created at or after the boundary remain HMAC-only. The endpoint
    is also rate-limited and only mutates active (non-terminal) jobs.
    """
    auth = request.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip()
    expected = compute_job_heartbeat_secret(job_id)
    # Compare as bytes (constant-time): hmac.compare_digest raises on a str with
    # non-ASCII chars, so a hostile Authorization header must not 500 the endpoint.
    hmac_valid = hmac.compare_digest(bearer.encode("utf-8"), expected.encode("ascii"))
    job = None
    if not hmac_valid:
        # Fetch only when the primary secret failed: old VMs must keep their
        # heartbeat lease through the deploy, while unknown UUIDs still receive
        # the same generic 403 as other invalid credentials.
        job = await session.get(ComputeJob, job_id)
        if job is None or not _accepts_legacy_compute_heartbeat(job, bearer):
            raise HTTPException(status_code=403, detail="Invalid compute job token")

    if job is None:
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
        _now = _utcnow()
        job.polling_metadata = append_progress_sample(
            dict(job.polling_metadata or {}),
            metric=metric,
            value=value,
            t=_now.isoformat(),
            received_at=_now,
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


# ── Viewable file upload from GCP compute-job VMs ──────────────────────
#
# The GCP VM runs a background watcher (viewable_watch.py) that polls the
# job's BLUEPRINT_ARTIFACT_DIR for files matching viewable extensions
# (png/jpg/jpeg/webp/gif/mp4/webm) and POSTs each to this endpoint using
# the per-job HMAC secret for authentication.  The endpoint enforces the
# same policies as the runner-subprocess viewable endpoint in runner.py:
# extension-based kind classification, per-kind byte caps (8 MiB for images,
# 512 MiB for videos), and the 25-file per-job count budget.  Terminal jobs
# are refused — mid-run uploads may only add evidence to a job that is still
# live.

# Margin added to the max file byte cap when sizing the multipart body
# ceiling, so multipart framing overhead (boundary markers, headers, trailing
# CRLFs) does not push a valid payload over the cap.  Copied from runner.py.
_MULTIPART_BODY_MARGIN = 1 * 1024 * 1024  # 1 MiB


def _parse_multipart_form(body: bytes, content_type: str) -> tuple[dict, dict]:
    """Parse a ``multipart/form-data`` body without requiring ``python-multipart``.

    Returns ``(fields, files)``: ``fields`` maps each non-file field name to its
    raw bytes value; ``files`` maps each file field name to its raw bytes content.
    This is a minimal boundary splitter sufficient for the fixed-shape body of a
    viewable-file upload (one file part + one filename field).  Mirrors the
    implementation in ``runner.py``.

    NOTE: this parser loads the entire body into memory.  For the GCP VM
    viewable watcher the body is typically small (images under 8 MiB), so
    the in-memory approach is fine for the initial implementation.  A future
    streaming path similar to runner's ``_parse_multipart_form_stream`` +
    ``_FileRangeReader`` can be added when large mid-run video uploads from
    GCP VMs are needed.
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
        if seg.startswith(b"--"):
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


@router.post("/compute-jobs/{job_id}/viewable")
@limiter.limit("120/minute")
async def upload_compute_job_viewable(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Receive a single viewable file from a GCP compute-job VM mid-execution.

    GCP VMs run a background watcher (``viewable_watch.py``) that polls
    ``$BLUEPRINT_ARTIFACT_DIR`` for new viewable-extension files and uploads each
    to this endpoint so they appear in the strategic canvas BEFORE the job reaches
    a terminal state.

    Authentication uses the same per-job HMAC secret as the heartbeat endpoint
    (see ``compute_job_heartbeat_secret``), embedded only in the encrypted startup
    script and never echoed by any API.

    Body is ``multipart/form-data``: a ``file`` field carrying the viewable file
    bytes, plus a ``filename`` form field with the relative path of the file
    (``reward_curve.png`` or ``arm1/reward.png`` — matching the terminal
    ``persist_viewable_artifacts`` pipeline).

    The endpoint enforces the same policies as the runner's viewable endpoint:
    extension-based classification, per-kind byte caps (8 MiB for images,
    512 MiB for videos), and the 25-file per-job count budget.
    """
    # ── Auth: same per-job HMAC secret as the heartbeat endpoint ────────
    auth = request.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip()
    expected = compute_job_heartbeat_secret(job_id)
    if not hmac.compare_digest(bearer.encode("utf-8"), expected.encode("ascii")):
        raise HTTPException(status_code=403, detail="Invalid compute job token")

    # ── Read the multipart body with a bounded spool ────────────────────
    # Small bodies (images) stay in memory; a large video body rolls to a
    # temp file so the orchestrator never buffers a multi-hundred-MiB payload
    # on the heap.  The hard cap covers the largest viewable file plus
    # multipart framing overhead.
    max_body_bytes = _MAX_VIEWABLE_VIDEO_BYTES + _MULTIPART_BODY_MARGIN
    spool_threshold = _MAX_VIEWABLE_FILE_BYTES + _MULTIPART_BODY_MARGIN
    spool = tempfile.SpooledTemporaryFile(max_size=spool_threshold)
    try:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="viewable file body exceeds upload cap",
                )
            spool.write(chunk)

        spool.seek(0)
        raw = spool.read()
        fields, files = _parse_multipart_form(
            raw, request.headers.get("content-type") or "",
        )
        file_bytes = files.get("file")
        if file_bytes is None:
            raise HTTPException(
                status_code=400, detail="missing 'file' field in multipart form"
            )
        file_length = len(file_bytes)
        if file_length == 0:
            raise HTTPException(
                status_code=400, detail="empty viewable file body"
            )

        raw_filename = (
            (fields.get("filename") or b"").decode("latin-1", "replace").strip()
        )
        if not raw_filename:
            raise HTTPException(
                status_code=400,
                detail="missing or empty 'filename' field",
            )

        # ── Extension-based classification ──────────────────────────────
        type_info = storage.viewable_content_type_for_filename(raw_filename)
        if type_info is None:
            raise HTTPException(
                status_code=422,
                detail=f"file type not viewable: {raw_filename!r} — "
                f"supported: png/jpg/jpeg/webp/gif/mp4/webm",
            )
        content_type, kind = type_info

        safe_name = storage.sanitize_viewable_relpath(raw_filename)
        if safe_name is None:
            raise HTTPException(
                status_code=422,
                detail=f"filename {raw_filename!r} sanitizes to an empty or unsafe name",
            )

        # ── Enforce per-kind byte cap ───────────────────────────────────
        size_cap = (
            _MAX_VIEWABLE_VIDEO_BYTES
            if kind == "video"
            else _MAX_VIEWABLE_FILE_BYTES
        )
        if file_length > size_cap:
            raise HTTPException(
                status_code=413,
                detail=f"viewable file {safe_name!r} ({file_length} bytes) "
                f"exceeds the {size_cap}-byte cap for kind {kind!r}",
            )

        # ── Load job, validate non-terminal ─────────────────────────────
        job = await session.get(ComputeJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Compute job not found")

        if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="compute job is terminal — "
                "mid-run viewable uploads are refused",
            )

        # ── Pre-check count cap (advisory — authoritative under lock) ────
        expected_uri = storage.compute_job_viewable_uri(job_id, safe_name)
        meta_snapshot = job.polling_metadata or {}
        existing_snapshot = meta_snapshot.get("viewable_files") or []

        def _is_registered(meta_dict: Any) -> bool:
            entries = (meta_dict or {}).get("viewable_files") or []
            if not isinstance(entries, list):
                return False
            return any(
                isinstance(e, dict) and e.get("storage_uri") == expected_uri
                for e in entries
            )

        if (
            isinstance(existing_snapshot, list)
            and len(existing_snapshot) >= _MAX_VIEWABLE_FILE_COUNT
            and not _is_registered(meta_snapshot)
        ):
            raise HTTPException(
                status_code=429,
                detail=f"viewable file cap ({_MAX_VIEWABLE_FILE_COUNT}) "
                f"reached for this job",
            )

        # ── Upload to GCS ───────────────────────────────────────────────
        uploaded_uri = await storage.upload_compute_job_viewable_file(
            job_id, safe_name, file_bytes, content_type,
        )
        if uploaded_uri is None:
            logger.warning(
                "[AGENT_JOBS] GCS upload failed for viewable file %r on "
                "job %s — storage unavailable",
                safe_name,
                job_id,
            )
            raise HTTPException(
                status_code=502,
                detail="viewable file upload to storage failed",
            )

    finally:
        spool.close()

    # ── Re-read with row lock — re-check terminal + count cap ──────────
    result = await session.execute(
        select(ComputeJob)
        .where(ComputeJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job vanished mid-upload")

    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        logger.warning(
            "[AGENT_JOBS] Compute job %s went terminal while viewable "
            "file %r upload was in flight — discarding unreferenced object",
            job_id,
            safe_name,
        )
        await storage.delete_compute_job_viewable_file(job_id, safe_name)
        raise HTTPException(
            status_code=409,
            detail="compute job went terminal during upload — "
            "live artifact was discarded",
        )

    # ── Update polling_metadata viewable_files list ────────────────────
    meta = dict(job.polling_metadata or {})

    raw_existing_list = meta.get("viewable_files") or []
    if not isinstance(raw_existing_list, list):
        raw_existing_list = []
    old_list = raw_existing_list
    # Detach nested JSON dicts from the ORM-owned polling_metadata value.
    existing_list = [
        dict(item) if isinstance(item, dict) else item
        for item in old_list
    ]

    # Dedup by storage_uri
    existing_entry = None
    for manifest_entry in existing_list:
        if (
            isinstance(manifest_entry, dict)
            and manifest_entry.get("storage_uri") == uploaded_uri
        ):
            existing_entry = manifest_entry
            break

    # Count cap (definitive check under lock)
    if existing_entry is None and len(existing_list) >= _MAX_VIEWABLE_FILE_COUNT:
        await storage.delete_compute_job_viewable_file(job_id, safe_name)
        raise HTTPException(
            status_code=429,
            detail=f"viewable file cap ({_MAX_VIEWABLE_FILE_COUNT}) "
            f"reached for this job",
        )

    revision = storage.new_viewable_revision()

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
        existing_entry.update(entry)
    else:
        existing_list.append(entry)
    meta["viewable_files"] = existing_list

    # ── Create/update ComputeJobFile signing row ────────────────────────
    file_row = (
        await session.execute(
            select(ComputeJobFile).where(
                ComputeJobFile.storage_uri == uploaded_uri,
            )
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
                "[AGENT_JOBS] ComputeJobFile insert failed for viewable "
                "file %r in job=%s — continuing without DB row",
                safe_name,
                job.id,
                exc_info=True,
            )

    job.polling_metadata = meta
    job.updated_at = _utcnow()
    session.add(job)
    await session.commit()

    return {
        "ok": True,
        "entry": entry,
        "total_viewable_files": len(existing_list),
    }
