"""Operator-facing compute-job monitoring and kill endpoints.

A read endpoint lists a project's live (and recently-finished) compute jobs for
the operator dashboard, and a guarded kill endpoint stops a running job on
demand. Both are project-scoped through ``_get_user_project_with_role``: listing
needs viewer, killing needs member.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.compute_jobs import (
    build_compute_result_logs_async,
    kill_compute_job,
    list_compute_jobs,
)
from app.compute_jobs_aggregation import (
    aggregate_all_compute_jobs_summary,
    aggregate_compute_jobs_summary,
    aggregate_criterion_summary,
)
from app.database import get_session
from app.models import Artifact, ComputeJob, ComputeJobStatus, LocalRunner, Task
from app.schemas import serialize_compute_job_response_safe
from app.strategic_runtime_work import (
    local_runner_hold_task_filters,
    task_awaiting_local_runner_since,
)
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
_ASK_HISTORY_MAX_TURNS = 6
_ASK_HISTORY_TURN_MAX_CHARS = 1000


class _ComputeJobAskHistoryTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(..., min_length=1, max_length=_ASK_HISTORY_TURN_MAX_CHARS)
    answer: str = Field(..., min_length=1, max_length=_ASK_HISTORY_TURN_MAX_CHARS)


class _AskComputeJobRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(..., min_length=1)
    history: list[_ComputeJobAskHistoryTurn] = Field(default_factory=list)

    @field_validator("history", mode="before")
    @classmethod
    def _sanitize_history(cls, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        turns: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            answer = item.get("answer")
            if not isinstance(question, str) or not isinstance(answer, str):
                continue
            question = question[:_ASK_HISTORY_TURN_MAX_CHARS]
            answer = answer[:_ASK_HISTORY_TURN_MAX_CHARS]
            if question and answer:
                turns.append({"question": question, "answer": answer})
        return turns[-_ASK_HISTORY_MAX_TURNS:]

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


def _viewable_artifacts_descriptor(raw: Any) -> list[Dict[str, Any]]:
    """Normalize a persisted viewable-file manifest into the operator-facing list.

    Reads the ``viewable_files`` manifest stamped onto ``polling_metadata`` by
    ``_link_terminal_artifact`` and returns a sanitized, backward-compatible list
    of ``{filename, storage_uri, content_type, size_bytes, kind}``. A signed
    preview URL is resolved lazily via ``/api/admin/artifacts/download-url`` from
    ``storage_uri`` (which lives inside the allowlisted compute-job prefix), so no
    URL is minted here. Returns ``[]`` when a job produced no viewable files or
    the manifest is malformed (absent/empty is the common, expected case).
    """
    if not isinstance(raw, list):
        return []
    out: list[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        storage_uri = entry.get("storage_uri")
        if not isinstance(storage_uri, str) or not storage_uri:
            continue
        filename = entry.get("filename")
        content_type = entry.get("content_type")
        size_bytes = entry.get("size_bytes")
        kind = entry.get("kind")
        out.append(
            {
                "filename": str(filename) if filename is not None else None,
                "storage_uri": storage_uri,
                "content_type": content_type
                if isinstance(content_type, str)
                else None,
                "size_bytes": int(size_bytes)
                if isinstance(size_bytes, int) and not isinstance(size_bytes, bool)
                else None,
                "kind": kind if kind in ("image", "video") else None,
            }
        )
    return out


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
            (
                await session.execute(
                    select(Task)
                    .options(load_only(Task.id, Task.payloads, Task.title, Task.plan_output, Task.context, raiseload=True))
                    .where(Task.id.in_(task_ids))
                )
            )
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

    # Batch-load LocalRunner rows for jobs that carry a runner_target_id in
    # their polling_metadata. One query covers all jobs in the page.
    runner_target_ids: set[uuid.UUID] = set()
    for job in jobs:
        raw_id = (job.polling_metadata or {}).get("runner_target_id")
        if raw_id:
            try:
                runner_target_ids.add(uuid.UUID(str(raw_id)))
            except (ValueError, AttributeError):
                pass
    runner_map: Dict[uuid.UUID, LocalRunner] = {}
    if runner_target_ids:
        runner_rows = (
            (
                await session.execute(
                    select(LocalRunner).where(LocalRunner.id.in_(runner_target_ids))
                )
            )
            .scalars()
            .all()
        )
        runner_map = {r.id: r for r in runner_rows}

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
            artifact_text = await build_compute_result_logs_async(job)
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
        launch_params: dict | None = None
        if task_obj is not None:
            fp = task_obj.first_payload
            raw_arms = fp.get("arms")
            arms_val = raw_arms if isinstance(raw_arms, list) else None
            raw_env = fp.get("env")
            env_key_names = sorted(raw_env.keys()) if isinstance(raw_env, dict) else None
            raw_provenance = fp.get("provenance")
            raw_spend = fp.get("spend_cap_usd")
            raw_deadline = fp.get("deadline_seconds")
            launch_params = {
                "command": fp.get("command") or None,
                "arms": arms_val,
                "declared_metric": fp.get("declared_metric") or None,
                "metric": fp.get("metric") or None,
                "cwd": fp.get("cwd") or None,
                "env_keys": env_key_names,
                "accelerator": fp.get("accelerator") or None,
                "machine_type": fp.get("machine_type") or None,
                "container_image": fp.get("container_image") or None,
                "provisioning_model": fp.get("provisioning_model") or None,
                "provenance": raw_provenance if isinstance(raw_provenance, dict) else None,
                "spend_cap_usd": float(raw_spend) if raw_spend is not None else None,
                "deadline_seconds": int(raw_deadline) if raw_deadline is not None else None,
                "require_live_release": bool(fp.get("require_live_release")) if fp.get("require_live_release") is not None else None,
                "require_durable_artifact": bool(fp.get("require_durable_artifact")) if fp.get("require_durable_artifact") is not None else None,
            }
        data["task_title"] = title
        data["task_type"] = task_type
        data["success_criterion"] = success_criterion
        data["criterion_verdict"] = criterion_verdict
        data["knowledge_document_id"] = knowledge_document_id
        data["per_arm_verdicts"] = per_arm_verdicts
        data["experiment"] = experiment
        data["benchmark"] = benchmark
        data["hypothesis"] = hypothesis
        data["launch_params"] = launch_params
        linked_artifact = artifacts.get(job.artifact_id) if job.artifact_id else None
        data["artifact"] = (
            _artifact_descriptor(linked_artifact) if linked_artifact else None
        )
        # Runner identity and wait-state fields.
        pm = job.polling_metadata or {}
        # Additive per-file viewable-artifact manifest (empty when the job
        # produced no viewable files) so the UI can enumerate individual
        # image/video references alongside the opaque bundle `artifact`.
        data["viewable_artifacts"] = _viewable_artifacts_descriptor(
            pm.get("viewable_files")
        )
        raw_runner_id = pm.get("runner_target_id")
        runner_target_id_str: str | None = str(raw_runner_id) if raw_runner_id else None
        data["runner_target_id"] = runner_target_id_str
        data["runner_claimed_at"] = pm.get("runner_claimed_at")
        runner_descriptor: Dict[str, Any] | None = None
        if runner_target_id_str:
            try:
                runner_obj = runner_map.get(uuid.UUID(runner_target_id_str))
                if runner_obj is not None:
                    runner_descriptor = {
                        "id": str(runner_obj.id),
                        "hostname": runner_obj.hostname,
                        "status": runner_obj.status,
                    }
            except (ValueError, AttributeError):
                pass
        data["runner"] = runner_descriptor
        # awaiting_local_runner_since lives on the launching task's context, not
        # the job row. The task_obj is already loaded in the batch above.
        awaiting: str | None = None
        if task_obj is not None:
            raw_awaiting = (task_obj.context or {}).get("awaiting_local_runner_since")
            awaiting = str(raw_awaiting) if raw_awaiting else None
        data["awaiting_local_runner_since"] = awaiting
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


def _awaiting_local_runner_entry(task: Task) -> Dict[str, Any]:
    """Serialize a compute_experiment task held awaiting a local runner.

    These tasks have NO ComputeJob row (the orchestrator holds them QUEUED
    before any job is created), so this is the only surface that exposes them.
    The response shape is a stable contract the compute-workspace UI depends on:

        {
          "id": str,                            # task id
          "title": str | None,
          "awaiting_local_runner_since": str,   # ISO stamp from task.context
          "created_at": str | None,             # ISO
          "requires_approval": bool,
          "spend_cap_usd": float | None,        # launch essentials from payload
          "command": str | None,
          "machine_type": str | None,
          "accelerator": str | None,
          "container_image": str | None,
          "provisioning_model": str | None,
          "deadline_seconds": int | None,
        }
    """
    fp = task.first_payload
    raw_spend = fp.get("spend_cap_usd")
    raw_deadline = fp.get("deadline_seconds")
    return {
        "id": str(task.id),
        "title": task.title,
        "awaiting_local_runner_since": task_awaiting_local_runner_since(task),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "requires_approval": bool(task.requires_approval),
        "spend_cap_usd": float(raw_spend) if raw_spend is not None else None,
        "command": fp.get("command") or None,
        "machine_type": fp.get("machine_type") or None,
        "accelerator": fp.get("accelerator") or None,
        "container_image": fp.get("container_image") or None,
        "provisioning_model": fp.get("provisioning_model") or None,
        "deadline_seconds": int(raw_deadline) if raw_deadline is not None else None,
    }


@router.get("/projects/{project_id}/compute-jobs/awaiting-local-runner")
async def get_project_awaiting_local_runner_holds(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List compute_experiment tasks held QUEUED awaiting a local runner (viewer+).

    When project policy requires local execution and no eligible runner is live,
    the orchestrator holds a ``compute_experiment`` task in QUEUED with an
    ``awaiting_local_runner_since`` stamp on ``task.context`` and never creates a
    ComputeJob row. The regular ``/compute-jobs`` listing only surfaces ComputeJob
    rows, so these blocked experiments are otherwise invisible in the workspace.

    Returns a JSON list (newest ``created_at`` first). The filter is the shared
    ``local_runner_hold_task_filters`` predicate: QUEUED, not archived,
    ``requires_approval`` is not True, payload type ``compute_experiment``, and a
    non-empty awaiting stamp. Each entry's shape is documented on
    ``_awaiting_local_runner_entry`` and is a stable contract for the UI.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    rows = (
        (
            await session.execute(
                select(Task)
                .where(*local_runner_hold_task_filters(project_id))
                .order_by(Task.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    return [_awaiting_local_runner_entry(task) for task in rows]


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
    built = await _build_compute_job_payload(session, [killed])
    return {"compute_job": built[0]}


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


@router.post("/compute-jobs/{job_id}/ask")
async def ask_compute_job(
    job_id: uuid.UUID,
    body: _AskComputeJobRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Answer a free-form question grounded in a terminal compute job's evidence (member+)."""
    job = await session.get(ComputeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Compute job not found")
    await _get_user_project_with_role(
        job.project_id, current_user["user_id"], session, "member"
    )
    if job.status not in _COMPUTE_JOB_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Compute job is not terminal (status={job.status}); Q&A requires a terminal job",
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

    logs = json.dumps({"job_id": str(job.id)})

    from app.knowledge_completion import answer_compute_experiment_question

    return await answer_compute_experiment_question(
        task,
        question=body.question,
        outcome=outcome,
        cost=cost,
        criterion=criterion,
        result_reference=job.result_reference,
        session=session,
        logs=logs,
        history=[turn.model_dump() for turn in body.history],
    )


# ── GPU contamination audit ───────────────────────────────────────────────────

# Commit that introduced /sys ro-bind for bwrap GPU sandboxes (PR #14183).
# Jobs that STARTED before this timestamp on local-runner may have been silently
# running on CPU even when an accelerator was declared (cuInit returned rc=304).
_GPU_SYS_FIX_UTC = datetime(2026, 7, 11, 7, 6, 21, tzinfo=timezone.utc)
_RUNNER_SUBPROCESS_BACKEND = "runner-subprocess"


@router.get("/compute-jobs/gpu-contamination-audit")
async def gpu_contamination_audit(
    project_id: uuid.UUID = Query(...),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """List accelerator-declared local-runner compute jobs started BEFORE the
    /sys ro-bind fix (PR #14183, commit b92e368) that may have silently run on
    CPU instead of GPU.

    These jobs should be treated as suspect: CUDA likely failed inside the bwrap
    sandbox with rc=304 CUDA_ERROR_OPERATING_SYSTEM, causing torch/TF to
    silently fall back to CPU while reporting a successful exit code.

    Returns a read-only report; no data is deleted or modified.
    """
    await _get_user_project_with_role(project_id, user, session, required_role="viewer")

    from sqlalchemy import and_

    result = await session.execute(
        select(ComputeJob)
        .where(
            and_(
                ComputeJob.project_id == project_id,
                ComputeJob.backend == _RUNNER_SUBPROCESS_BACKEND,
                ComputeJob.accelerator_type.isnot(None),
                ComputeJob.started_at.isnot(None),
                ComputeJob.started_at < _GPU_SYS_FIX_UTC,
            )
        )
        .order_by(ComputeJob.started_at.asc())
        .limit(500)
    )
    jobs = result.scalars().all()

    task_ids = {j.task_id for j in jobs if j.task_id is not None}
    task_titles: Dict[uuid.UUID, str | None] = {}
    if task_ids:
        task_rows = (
            await session.execute(
                select(Task)
                .options(load_only(Task.id, Task.title, raiseload=True))
                .where(Task.id.in_(task_ids))
            )
        ).scalars().all()
        task_titles = {t.id: t.title for t in task_rows}

    items = []
    for job in jobs:
        items.append({
            "job_id": str(job.id),
            "task_id": str(job.task_id) if job.task_id else None,
            "task_title": task_titles.get(job.task_id) if job.task_id else None,
            "accelerator_type": job.accelerator_type,
            "status": job.status.value if job.status else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "ended_at": job.ended_at.isoformat() if job.ended_at else None,
            "suspect_reason": (
                "started before /sys ro-bind fix (PR #14183); cuInit likely "
                "returned CUDA_ERROR_OPERATING_SYSTEM (rc=304), causing silent "
                "CPU fallback"
            ),
        })

    return {
        "fix_landed_at": _GPU_SYS_FIX_UTC.isoformat(),
        "fix_pr": "#14183",
        "fix_commit": "b92e36823",
        "suspect_count": len(items),
        "jobs": items,
    }
