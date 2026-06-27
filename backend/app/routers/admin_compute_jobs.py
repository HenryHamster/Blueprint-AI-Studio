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
from app.compute_jobs import (
    build_compute_result_logs,
    kill_compute_job,
    list_compute_jobs,
)
from app.compute_jobs_aggregation import (
    aggregate_all_compute_jobs_summary,
    aggregate_compute_jobs_summary,
    aggregate_criterion_summary,
)
from app.database import get_session
from app.models import Artifact, ComputeJob, ComputeJobStatus, Task
from app.schemas import serialize_compute_job_response_safe
from app.task_evaluators import (
    _declared_success_criterion,
    evaluate_compute_success_criterion,
)

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


async def _build_compute_job_payload(
    session: AsyncSession, jobs
) -> list[Dict[str, Any]]:
    """Serialize jobs and augment each with the launching task's title/type,
    an optional descriptor for the job's linked durable Artifact, and the
    declared success criterion + its computed verdict when present."""
    task_ids = {job.task_id for job in jobs if job.task_id is not None}
    # Maps task_id -> (title, task_type, success_criterion, criterion_verdict, task)
    task_meta: Dict[uuid.UUID, tuple[str | None, str | None, Any, Any, Any]] = {}
    if task_ids:
        rows = (
            (await session.execute(select(Task).where(Task.id.in_(task_ids))))
            .scalars()
            .all()
        )
        for task in rows:
            task_type = str(task.first_payload.get("type") or "") or None
            normalized = _declared_success_criterion(task)
            success_criterion = (
                {
                    "metric": normalized[0],
                    "operator": normalized[1],
                    "threshold": normalized[2],
                }
                if normalized is not None
                else None
            )
            plan_output = task.plan_output or {}
            compute_result = plan_output.get("compute_result") or {}
            verdict = compute_result.get("criterion")
            if not isinstance(verdict, dict):
                verdict = None
            task_meta[task.id] = (
                task.title,
                task_type,
                success_criterion,
                verdict,
                task,
            )

    # Batch-load linked artifacts to avoid an N+1 query across the listed jobs.
    artifact_ids = {job.artifact_id for job in jobs if job.artifact_id is not None}
    artifacts: Dict[uuid.UUID, Artifact] = {}
    if artifact_ids:
        artifact_rows = (
            (
                await session.execute(
                    select(Artifact).where(Artifact.id.in_(artifact_ids))
                )
            )
            .scalars()
            .all()
        )
        artifacts = {artifact.id: artifact for artifact in artifact_rows}

    payload: list[Dict[str, Any]] = []
    for job in jobs:
        data = serialize_compute_job_response_safe(job)
        title, task_type, success_criterion, criterion_verdict, task_obj = (
            task_meta.get(job.task_id, (None, None, None, None, None))
        )
        # For terminal jobs whose task hasn't been finalized yet (plan_output
        # does not carry compute_result.criterion), compute the verdict directly
        # from the job row so the panel shows real results immediately.
        if (
            criterion_verdict is None
            and success_criterion is not None
            and job.status in _COMPUTE_JOB_TERMINAL_STATUSES
            and task_obj is not None
        ):
            artifact_text = build_compute_result_logs(job)
            criterion_verdict = evaluate_compute_success_criterion(
                task_obj, artifact_text
            )
        # Derive knowledge_document_id from the task's plan_output.
        knowledge_document_id: str | None = None
        per_arm_verdicts: dict | None = None
        if task_obj is not None:
            knowledge_doc = (task_obj.plan_output or {}).get("knowledge_document") or {}
            raw_kdoc_id = (
                knowledge_doc.get("id") if isinstance(knowledge_doc, dict) else None
            )
            knowledge_document_id = (
                str(raw_kdoc_id) if raw_kdoc_id is not None else None
            )
            raw_pav = knowledge_doc.get("per_arm_verdicts") if isinstance(knowledge_doc, dict) else None
            per_arm_verdicts = raw_pav if isinstance(raw_pav, dict) else None
        experiment: str | None = None
        benchmark: str | None = None
        hypothesis: str | None = None
        if task_obj is not None:
            fp = task_obj.first_payload
            raw_experiment = fp.get("experiment")
            raw_benchmark = fp.get("benchmark")
            raw_hypothesis = fp.get("hypothesis")
            experiment = str(raw_experiment) if raw_experiment else None
            benchmark = str(raw_benchmark) if raw_benchmark else None
            hypothesis = str(raw_hypothesis) if raw_hypothesis else None
        data["task_title"] = title
        data["task_type"] = task_type
        data["success_criterion"] = success_criterion
        data["criterion_verdict"] = criterion_verdict
        data["knowledge_document_id"] = knowledge_document_id
        data["per_arm_verdicts"] = per_arm_verdicts
        data["experiment"] = experiment
        data["benchmark"] = benchmark
        data["hypothesis"] = hypothesis
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
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    all_terminal: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List a project's active and recently-finished compute jobs (viewer+).

    When ``all_terminal=true``, all terminal jobs are returned regardless of
    age — ``include_finished_minutes`` is accepted but ignored for the terminal
    filter, allowing deep-history browsing of past experiments.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    window = timedelta(minutes=include_finished_minutes)
    jobs, has_more = await list_compute_jobs(
        session,
        project_id=project_id,
        include_finished_within=window,
        limit=limit,
        offset=offset,
        all_terminal=all_terminal,
    )
    built_payload = await _build_compute_job_payload(session, jobs)
    agg_summary = await aggregate_compute_jobs_summary(
        session,
        project_id=project_id,
        include_finished_within=window,
        all_terminal=all_terminal,
    )
    criterion_stats = await aggregate_criterion_summary(
        session,
        project_id=project_id,
        include_finished_within=window,
        all_terminal=all_terminal,
    )
    summary = {
        **agg_summary,
        "criterion_met": criterion_stats["criterion_met"],
        "criterion_measured": criterion_stats["criterion_measured"],
    }
    return {
        "compute_jobs": built_payload,
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
        "summary": summary,
    }


@router.get("/projects/{project_id}/compute-jobs/summary")
async def get_project_compute_jobs_aggregate_summary(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return aggregate summary over ALL compute jobs for a project (viewer+).

    Unlike the list endpoint, this endpoint is not capped by include_finished_minutes
    and covers the project's full history — suitable for cumulative experiment totals.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    return await aggregate_all_compute_jobs_summary(session, project_id=project_id)


@router.get("/compute-jobs/{job_id}")
async def get_compute_job_by_id(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Fetch a single compute job by id (viewer+).

    Returns the full job detail including its per-arm result block, task title,
    success criterion, and criterion verdict. Normalizes 403 to 404 so job
    existence is not leaked to callers without viewer access (IDOR protection).
    """
    job = await session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")
    try:
        await _get_user_project_with_role(
            job.project_id, current_user["user_id"], session, "viewer"
        )
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="Compute job not found") from exc
    built = await _build_compute_job_payload(session, [job])
    return {"compute_job": built[0]}


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
        logger.warning(
            "Validation error killing compute job: %s: %s", type(exc).__name__, exc
        )
        raise HTTPException(status_code=409, detail="Invalid request") from exc
    if killed is None:
        # A peer drove the job terminal during the lock-free RPC window; re-fetch
        # the now-terminal job so the caller still gets the final state.
        killed = await session.get(ComputeJob, job_id)
        if killed is None:
            raise HTTPException(status_code=404, detail="Compute job not found")
    return {"compute_job": serialize_compute_job_response_safe(killed)}


@router.get("/projects/{project_id}/compute-jobs/sweep-groups")
async def get_project_compute_sweep_groups(
    project_id: uuid.UUID,
    direction_id: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return durable sweep-grouped knowledge docs for a project (viewer+).

    Delegates to ``build_compute_sweep_groups`` which groups completed
    compute_experiment knowledge documents by ``sweep_id``.  Only sweeps with
    at least two docs are returned so the operator sees meaningful comparisons.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    from app.knowledge_query import build_compute_sweep_groups

    sweep_groups = await build_compute_sweep_groups(
        session,
        project_id,
        direction_id=direction_id,
        limit=limit,
    )
    return {"sweep_groups": sweep_groups}


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
