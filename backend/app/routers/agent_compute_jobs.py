"""Agent-facing compute-job launch and polling endpoints."""

import uuid
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.compute_jobs import create_compute_job, get_compute_job_for_task
from app.database import get_session
from app.models import Task
from app.routers.agent import verify_agent_token
from app.schemas import serialize_compute_job_response
from app.shared_state import limiter
from app.vm_cost_rates import DEFAULT_MACHINE_TYPE, supported_machine_types

router = APIRouter(prefix="/api/agent", tags=["agent"])

_COMPUTE_JOB_CAPABLE_TASK_TYPES = frozenset({"compute_experiment"})


class LaunchComputeJobRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = Field(..., min_length=1, max_length=10000)
    machine_type: str = Field(default=DEFAULT_MACHINE_TYPE, min_length=1, max_length=64)
    spend_cap_usd: float = Field(..., gt=0)
    deadline_seconds: int = Field(..., gt=0)
    cwd: str | None = Field(default=None, max_length=1000)
    env: Dict[str, str] | None = None
    backend_name: str | None = Field(default=None, max_length=32)
    provisioning_model: str | None = Field(default=None, max_length=16)


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

    if req.machine_type not in supported_machine_types():
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
    if task_type not in _COMPUTE_JOB_CAPABLE_TASK_TYPES:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "task_type_not_allowed_for_compute_jobs",
                "task_type": task_type,
                "allowed_task_types": sorted(_COMPUTE_JOB_CAPABLE_TASK_TYPES),
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
