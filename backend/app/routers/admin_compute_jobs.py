"""Operator-facing compute-job monitoring and kill endpoints.

A read endpoint lists a project's live (and recently-finished) compute jobs for
the operator dashboard, and a guarded kill endpoint stops a running job on
demand. Both are project-scoped through ``_get_user_project_with_role``: listing
needs viewer, killing needs member.
"""

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
from app.models import Artifact, ComputeJob, Task
from app.schemas import serialize_compute_job_response

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-compute-jobs"],
    dependencies=[Depends(get_current_user)],
)


def _redact_operator_polling_metadata(data: dict) -> dict:
    """Remove secret-bearing launch fields from the operator-facing response.

    Operators can see which env vars were set and all diagnostic/results keys,
    but not the raw values (which may carry tokens/secrets), the launch command,
    or the working directory. The agent-facing serializer is untouched — agents
    still receive the full polling_metadata to actually execute the job.
    """
    pm = data.get("polling_metadata")
    if not isinstance(pm, dict):
        return data
    # Remove fields that operators have no legitimate need for.
    pm.pop("command", None)
    pm.pop("cwd", None)
    # Redact env values; keep keys so operators can see which variables were set.
    if isinstance(pm.get("env"), dict):
        pm["env"] = {k: "[REDACTED]" for k in pm["env"]}
    return data


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
        data = _redact_operator_polling_metadata(serialize_compute_job_response(job))
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
    await _get_user_project_with_role(
        job.project_id, current_user["user_id"], session, "member"
    )
    try:
        killed = await kill_compute_job(
            session,
            job_id=job_id,
            project_id=job.project_id,
            killed_by=current_user.get("email") or current_user.get("user_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if killed is None:
        # A peer drove the job terminal during the lock-free RPC window; re-fetch
        # the now-terminal job so the caller still gets the final state.
        killed = await session.get(ComputeJob, job_id)
        if killed is None:
            raise HTTPException(status_code=404, detail="Compute job not found")
    return {"compute_job": _redact_operator_polling_metadata(serialize_compute_job_response(killed))}
