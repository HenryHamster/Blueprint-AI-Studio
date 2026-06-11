"""Agent-facing compute-job launch and polling endpoints."""

import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.compute_jobs import (
    ComputeLaunchPreconditionFailed,
    build_compute_result_logs,
    create_compute_job,
    get_compute_job_for_task,
)
from app.database import get_session
from app.models import Task
from app.routers.agent import verify_agent_token
from app.schemas import serialize_compute_job_response
from app.shared_state import limiter
from app.task_capabilities import COMPUTE_JOB_LAUNCH_TASK_TYPES
from app.vm_cost_rates import resolve_compute_machine_type, supported_machine_types

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

# Context key carrying the VM-substrate analysis request bundle stamped by
# task_completion._route_compute_analysis_to_vm. Mirrored here (not imported) to
# avoid a router → task_completion import cycle; the string is the contract.
_COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY = "compute_vm_analysis_request"


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
        )
    except ComputeLaunchPreconditionFailed as exc:
        raise HTTPException(status_code=412, detail=exc.detail) from exc
    except ValueError as exc:
        logger.warning("Validation error launching compute job: %s: %s", type(exc).__name__, exc)
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


class ComputeAnalysisDocument(BaseModel):
    """The cited analysis JSON the agent VM produced with the centralized CLI."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, max_length=500)
    summary: str = Field(..., min_length=1, max_length=20000)
    body: str = Field(..., min_length=1, max_length=200000)
    source_citations: list[str] = Field(default_factory=list, max_length=100)


class SubmitComputeAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: uuid.UUID
    analysis: ComputeAnalysisDocument


@router.post("/compute-analysis")
@limiter.limit("30/minute")
async def submit_compute_analysis(
    request: Request,
    req: SubmitComputeAnalysisRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Accept VM-produced compute analysis and upgrade the KnowledgeDocument.

    The agent VM ran the model on the centralized-accounts substrate (operator
    policy p-13f153e9 forbids a backend ANTHROPIC_API_KEY) and POSTs the produced
    analysis JSON here. Citation grounding is re-enforced BACKEND-SIDE against the
    real durable artifact — the VM's citations are never trusted — and a grounded
    analysis upgrades the deterministic doc to ``analysis_mode='llm'`` through the
    existing upgrade-only persistence machinery. Additive endpoint: it does not
    change POST /api/agent/status or the admin analyze endpoint.
    """
    task_id_raw = token.get("sub")
    if not task_id_raw:
        raise HTTPException(status_code=403, detail="Invalid token: missing sub")
    try:
        task_id = uuid.UUID(task_id_raw)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid token: bad sub") from exc

    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    # Resolve the terminal compute job for THIS task so the analysis inputs and
    # citation-grounding evidence are derived authoritatively from the durable job
    # row — never from anything the VM supplies beyond the produced prose.
    job = await get_compute_job_for_task(
        session, job_id=req.job_id, task_id=task_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")

    compute_result = (task.plan_output or {}).get("compute_result") or {}
    outcome = str(compute_result.get("outcome") or "")
    if not outcome and job.outcome is not None:
        outcome = getattr(job.outcome, "value", str(job.outcome))
    if not outcome:
        outcome = "unknown"
    raw_cost = compute_result.get("final_cost_usd")
    cost: float | None = float(raw_cost) if raw_cost is not None else None
    criterion = compute_result.get("criterion")
    if not isinstance(criterion, dict):
        criterion = (task.context or {}).get("compute_success_criterion")
        if not isinstance(criterion, dict):
            criterion = None
    logs = build_compute_result_logs(job)

    from app.knowledge_completion import submit_compute_vm_analysis

    envelope = await submit_compute_vm_analysis(
        task,
        vm_document=req.analysis.model_dump(),
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=job.result_reference,
        session=session,
        logs=logs,
    )

    # Clear the one-shot request bundle so a re-claim does not re-enter analysis
    # mode. Best-effort and additive — never blocks the response.
    try:
        if (task.context or {}).get(_COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY) is not None:
            ctx = dict(task.context or {})
            ctx.pop(_COMPUTE_VM_ANALYSIS_REQUEST_CONTEXT_KEY, None)
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
    except Exception:  # noqa: BLE001 — bundle cleanup must never fail the submission
        logger.warning(
            "Failed to clear VM analysis request bundle for task %s", task_id,
            exc_info=True,
        )

    return {"status": "success", "data": envelope}
