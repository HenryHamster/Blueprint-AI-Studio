"""Operator-facing compute-job monitoring and kill endpoints.

A read endpoint lists a project's live (and recently-finished) compute jobs for
the operator dashboard, and a guarded kill endpoint stops a running job on
demand. Both are project-scoped through ``_get_user_project_with_role``: listing
needs viewer, killing needs member.
"""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.compute_jobs import kill_compute_job, list_compute_jobs
from app.database import get_session
from app.models import Artifact, ComputeJob, ComputeJobStatus, Task
from app.schemas import serialize_compute_job_response_safe

logger = logging.getLogger(__name__)

_COMPUTE_JOB_TERMINAL_STATUSES = frozenset(
    {
        ComputeJobStatus.SUCCEEDED,
        ComputeJobStatus.FAILED,
        ComputeJobStatus.CANCELLED,
        ComputeJobStatus.TIMED_OUT,
    }
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-compute-jobs"],
    dependencies=[Depends(get_current_user)],
)


def _artifact_descriptor(artifact: Artifact) -> Dict[str, Any]:
    """Build the additive operator-facing descriptor for a linked artifact.

    Exposes the artifact's identity and download affordance (downloads are
    resolved elsewhere via /api/admin/artifacts/download-url by storage_uri),
    so the descriptor only needs to carry the identifying fields.
    """
    # artifact_type is persisted as a plain String column, so it reads back as a
    # str; tolerate either the Enum member or the raw value.
    return {
        "id": str(artifact.id),
        "artifact_type": getattr(
            artifact.artifact_type, "value", artifact.artifact_type
        ),
        "storage_uri": artifact.storage_uri,
        "logical_key": artifact.logical_key,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


async def _build_compute_job_payload(session: AsyncSession, jobs) -> list[Dict[str, Any]]:
    """Serialize jobs and augment each with the launching task's title/type and
    an optional descriptor for the job's linked durable Artifact."""
    task_ids = {job.task_id for job in jobs if job.task_id is not None}
    titles: Dict[uuid.UUID, tuple[str | None, str | None]] = {}
    if task_ids:
        rows = (
            await session.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        for task in rows:
            task_type = str(task.first_payload.get("type") or "") or None
            titles[task.id] = (task.title, task_type)

    # Batch-load linked artifacts to avoid an N+1 query across the listed jobs.
    artifact_ids = {job.artifact_id for job in jobs if job.artifact_id is not None}
    artifacts: Dict[uuid.UUID, Artifact] = {}
    if artifact_ids:
        artifact_rows = (
            await session.execute(
                select(Artifact).where(Artifact.id.in_(artifact_ids))
            )
        ).scalars().all()
        artifacts = {artifact.id: artifact for artifact in artifact_rows}

    payload: list[Dict[str, Any]] = []
    for job in jobs:
        data = serialize_compute_job_response_safe(job)
        title, task_type = titles.get(job.task_id, (None, None))
        data["task_title"] = title
        data["task_type"] = task_type
        linked_artifact = artifacts.get(job.artifact_id) if job.artifact_id else None
        data["artifact"] = (
            _artifact_descriptor(linked_artifact) if linked_artifact else None
        )
        payload.append(data)
    return payload


@router.get("/projects/{project_id}/compute-jobs")
async def get_project_compute_jobs(
    project_id: uuid.UUID,
    include_finished_minutes: int = Query(360, ge=0, le=10080),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List a project's active and recently-finished compute jobs (viewer+)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    jobs = await list_compute_jobs(
        session,
        project_id=project_id,
        include_finished_within=timedelta(minutes=include_finished_minutes),
    )
    return {"compute_jobs": await _build_compute_job_payload(session, jobs)}


@router.post("/compute-jobs/{job_id}/kill")
async def kill_project_compute_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Kill a running compute job on operator demand (member+)."""
    job = await session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")
    try:
        await _get_user_project_with_role(
            job.project_id, current_user["user_id"], session, "member"
        )
    except HTTPException as exc:
        # Normalize 403 to 404 so job existence is not leaked to callers who
        # lack kill rights (e.g. viewers of the project).
        raise HTTPException(status_code=404, detail="Compute job not found") from exc
    try:
        killed = await kill_compute_job(
            session,
            job_id=job_id,
            project_id=job.project_id,
            killed_by=current_user.get("email") or current_user.get("user_id"),
        )
    except ValueError as exc:
        logger.warning("Validation error killing compute job: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=409, detail="Invalid request") from exc
    if killed is None:
        # A peer drove the job terminal during the lock-free RPC window; re-fetch
        # the now-terminal job so the caller still gets the final state.
        killed = await session.get(ComputeJob, job_id)
        if killed is None:
            raise HTTPException(status_code=404, detail="Compute job not found")
    return {"compute_job": serialize_compute_job_response_safe(killed)}


@router.post("/compute-jobs/{job_id}/analyze")
async def analyze_project_compute_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Trigger optional LLM analysis for a terminal compute job (member+).

    Resolves outcome/cost/criterion from the durable job+task state so the
    analysis inputs can never disagree with what completion recorded. Returns
    exactly the envelope that ``analyze_compute_experiment_knowledge`` persisted.
    """
    job = await session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")
    await _get_user_project_with_role(
        job.project_id, current_user["user_id"], session, "member"
    )
    if job.status not in _COMPUTE_JOB_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Compute job is not terminal (status={job.status}); analysis requires a terminal job",
        )
    if job.task_id is None:
        raise HTTPException(status_code=409, detail="Compute job has no linked task")
    task = await session.get(Task, job.task_id)
    if task is None:
        raise HTTPException(status_code=409, detail="Linked task not found")

    compute_result = (task.plan_output or {}).get("compute_result") or {}
    outcome = str(compute_result.get("outcome") or "")
    if not outcome and job.outcome is not None:
        outcome = job.outcome.value
    if not outcome:
        outcome = "unknown"
    raw_cost = compute_result.get("final_cost_usd")
    cost: float | None = float(raw_cost) if raw_cost is not None else None
    criterion = compute_result.get("criterion")
    if not isinstance(criterion, dict):
        criterion = None

    # Synthesize logs so _gather_compute_evidence resolves the job row identically
    # to how completion did — the job_id is the stable pointer.
    logs = json.dumps({"job_id": str(job.id)})

    from app.knowledge_completion import analyze_compute_experiment_knowledge

    return await analyze_compute_experiment_knowledge(
        task,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=job.result_reference,
        session=session,
        logs=logs,
    )
