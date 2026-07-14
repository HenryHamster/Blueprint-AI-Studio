"""Activity feed endpoint: recent system events in reverse chronological order."""

import uuid
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session, resolve_dialect_name
from app.models import (
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    DeploymentState,
    _utcnow,
)
from app.auth import get_current_user
from app.admin import _user_project_ids
from app.failure_classification import classify_failure, classify_failure_taxonomy
from app.research_completion import resolve_research_findings_for_surface
from app.sr_review_quality_metrics import _stash_projected_task_fields

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-activity"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/activity")
async def get_activity(
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    artifact_limit: int = Query(200, ge=1, le=1000),
    event_types: List[str] | None = Query(None),
    since: datetime | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a reverse-chronological list of recent system events."""
    seven_days_ago = _utcnow() - timedelta(days=7)
    cutoff = since if since is not None else seven_days_ago
    events: List[Dict[str, Any]] = []

    # --- 1 & 2: Completed and Failed tasks in last 7 days (or since cutoff) ---
    user_project_ids = _user_project_ids(current_user["user_id"])
    terminal_activity_at = func.coalesce(
        Task.completed_at,
        Task.updated_at,
        Task.created_at,
    )
    task_filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        terminal_activity_at >= cutoff,
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    # Project only payloads[0] SQL-side instead of loading the whole payloads
    # JSONB array (which grows per attempt and is TOAST-able / multi-MB per
    # row). plan_output is dropped entirely here and fetched narrowly below
    # only for the handful of tasks that carry a RESEARCH_FINDINGS artifact.
    # context stays loaded: classify_failure()/classify_failure_taxonomy() read
    # the full dict for FAILED rows. SQLite (tests without --postgres) can't
    # return a parsed dict from payloads[0], so it falls back to loading the
    # array — matching the established digest projection pattern.
    use_projection = resolve_dialect_name(session) == "postgresql"

    async def _fetch_tasks_with_first_payload(*, extra_cols, filters, order_by):
        if use_projection:
            result = await session.execute(
                select(Task, Task.payloads[0].label("_projected_first_payload"))
                .where(*filters)
                .options(load_only(*extra_cols, raiseload=True))
                .order_by(order_by)
                .offset(offset)
                .limit(limit * 3)
            )
            tasks = []
            for row in result.all():
                _stash_projected_task_fields(row[0], first_payload=row[1] or {})
                tasks.append(row[0])
            return tasks
        result = await session.execute(
            select(Task)
            .where(*filters)
            .options(load_only(*extra_cols, Task.payloads, raiseload=True))
            .order_by(order_by)
            .offset(offset)
            .limit(limit * 3)
        )
        return result.scalars().all()

    recent_tasks = await _fetch_tasks_with_first_payload(
        extra_cols=[
            Task.id,
            Task.status,
            Task.title,
            Task.project_id,
            Task.created_at,
            Task.started_at,
            Task.completed_at,
            Task.updated_at,
            Task.context,
            Task.latest_status_message,
        ],
        filters=task_filters,
        order_by=terminal_activity_at.desc(),
    )

    task_ids = [t.id for t in recent_tasks] if recent_tasks else []

    # Fetch relevant artifacts in one query
    artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> artifact info
    research_artifact_map: Dict[
        str, Dict[str, Any]
    ] = {}  # task_id -> research artifact
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error logs
    investigation_report_map: Dict[str, str] = {}  # task_id -> summary text

    if task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_(
                    [
                        ArtifactType.PULL_REQUEST,
                        ArtifactType.ERROR_LOG,
                        ArtifactType.RESEARCH_FINDINGS,
                        ArtifactType.INVESTIGATION_REPORT,
                    ]
                ),
            )
            .limit(artifact_limit)
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                artifact_map[tid] = {
                    "pr_url": a.pr_url,
                    "repo_url": a.repo_url,
                    "commit_hash": a.commit_hash[:7] if a.commit_hash else None,
                }
            elif (
                a.artifact_type == ArtifactType.ERROR_LOG
                and a.logs
                and tid not in error_log_map
            ):
                error_log_map[tid] = a.logs[:300]
            elif a.artifact_type == ArtifactType.RESEARCH_FINDINGS:
                research_artifact_map[tid] = {
                    "artifact_created_at": a.created_at,
                    "artifact_logs": a.logs[:100000] if a.logs else None,
                }
            elif a.artifact_type == ArtifactType.INVESTIGATION_REPORT and a.logs:
                investigation_report_map[tid] = a.logs[:2000]

    # Fetch plan_output (JSONB, potentially large research-finding arrays) only
    # for the tasks that actually emit a RESEARCH_FINDINGS event, instead of
    # loading it for every terminal row in the feed.
    plan_output_map: Dict[str, Any] = {}  # task_id -> plan_output
    if research_artifact_map:
        research_uuids = [uuid.UUID(tid) for tid in research_artifact_map]
        plan_output_result = await session.execute(
            select(Task.id, Task.plan_output).where(Task.id.in_(research_uuids))
        )
        for row in plan_output_result.all():
            plan_output_map[str(row[0])] = row[1]

    for t in recent_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        pr_info = artifact_map.get(tid, {})

        # Match the SQL filter/order expression so recent rows cannot emit
        # stale event timestamps.
        completed_at = t.completed_at or t.updated_at or t.created_at

        duration_seconds = None
        if completed_at is not None and t.started_at is not None:
            duration_seconds = (completed_at - t.started_at).total_seconds()

        if t.status == TaskStatus.COMPLETED:
            ctx = t.context if isinstance(t.context, dict) else {}
            raw_changed = ctx.get("_pr_changed_files")
            changed_files = (
                [f for f in raw_changed if isinstance(f, str)][:10]
                if isinstance(raw_changed, list)
                else None
            )
            scope = ctx.get("scope") if isinstance(ctx.get("scope"), str) else None
            direction_id = (
                ctx.get("direction_id")
                if isinstance(ctx.get("direction_id"), str)
                else None
            )
            review_loop = ctx.get("review_loop")
            review_iterations = (
                review_loop.get("iteration")
                if isinstance(review_loop, dict)
                and isinstance(review_loop.get("iteration"), int)
                else None
            )
            events.append(
                {
                    "id": f"task-{tid}",
                    "type": "TASK_COMPLETED",
                    "timestamp": completed_at.isoformat() if completed_at else None,
                    "title": t.title or "Completed task",
                    "task_id": tid,
                    "task_type": task_type,
                    "pr_url": pr_info.get("pr_url"),
                    "repo_url": pr_info.get("repo_url"),
                    "commit_hash": pr_info.get("commit_hash"),
                    "error_summary": None,
                    "finding_count": None,
                    "deploy_sha": None,
                    "deploy_status": None,
                    "rollback_count": None,
                    "duration_seconds": duration_seconds,
                    "changed_files": changed_files,
                    "scope": scope,
                    "direction_id": direction_id,
                    "review_iterations": review_iterations,
                }
            )
        elif t.status == TaskStatus.FAILED:
            events.append(
                {
                    "id": f"task-{tid}",
                    "type": "TASK_FAILED",
                    "timestamp": completed_at.isoformat() if completed_at else None,
                    "title": t.title or "Failed task",
                    "task_id": tid,
                    "task_type": task_type,
                    "pr_url": None,
                    "repo_url": None,
                    "commit_hash": None,
                    "error_summary": error_log_map.get(tid),
                    "investigation_summary": investigation_report_map.get(tid),
                    "failure_class": classify_failure(t, error_log_map.get(tid)),
                    "taxonomy": classify_failure_taxonomy(t),
                    "finding_count": None,
                    "deploy_sha": None,
                    "deploy_status": None,
                    "rollback_count": None,
                    "duration_seconds": duration_seconds,
                }
            )

        # --- 3: Research findings events (one per task) ---
        if tid in research_artifact_map:
            ra = research_artifact_map[tid]
            ra_ts = ra["artifact_created_at"]
            events.append(
                {
                    "id": f"research-{tid}",
                    "type": "RESEARCH_FINDINGS",
                    "timestamp": ra_ts.isoformat() if ra_ts else None,
                    "title": f"Research findings: {t.title or tid}",
                    "task_id": tid,
                    "task_type": task_type,
                    "pr_url": None,
                    "repo_url": None,
                    "commit_hash": None,
                    "error_summary": None,
                    "finding_count": len(
                        resolve_research_findings_for_surface(
                            plan_output_map.get(tid), ra.get("artifact_logs")
                        )
                        or []
                    ),
                    "investigation_summary": investigation_report_map.get(tid),
                    "deploy_sha": None,
                    "deploy_status": None,
                    "rollback_count": None,
                }
            )

    # --- 3b: In-progress tasks (QUEUED / PROVISIONING / IN_PROGRESS) ---
    inprogress_filters = [
        Task.status.in_(
            [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
        ),
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        inprogress_filters.append(Task.project_id == project_id)
    if since is not None:
        inprogress_filters.append(Task.created_at >= since)

    inprogress_tasks = await _fetch_tasks_with_first_payload(
        extra_cols=[
            Task.id,
            Task.title,
            Task.started_at,
            Task.created_at,
            Task.latest_status_message,
        ],
        filters=inprogress_filters,
        order_by=Task.created_at.desc(),
    )

    for t in inprogress_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        events.append(
            {
                "id": f"inprogress-{tid}",
                "type": "TASK_IN_PROGRESS",
                "timestamp": t.started_at.isoformat()
                if t.started_at
                else t.created_at.isoformat(),
                "title": t.title or "In-progress task",
                "task_id": tid,
                "task_type": task_type,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "latest_status_message": t.latest_status_message,
                "pr_url": None,
                "repo_url": None,
                "commit_hash": None,
                "error_summary": None,
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
            }
        )

    # --- 4: Deploy event from DeploymentState singleton ---
    state = await session.get(DeploymentState, 1)
    if state and state.deployed_at:
        short_sha = state.current_sha[:7] if state.current_sha else None
        deploy_event: Dict[str, Any] = {
            "id": f"deploy-{short_sha or 'unknown'}",
            "type": "DEPLOY_EVENT",
            "timestamp": state.deployed_at.isoformat(),
            "title": f"Deploy {short_sha or 'unknown'} — {state.status}",
            "task_id": None,
            "task_type": None,
            "pr_url": None,
            "repo_url": None,
            "commit_hash": short_sha,
            "error_summary": None,
            "finding_count": None,
            "deploy_sha": short_sha,
            "deploy_status": str(state.status) if state.status else None,
            "rollback_count": state.rollback_count
            if (state.rollback_count or 0) > 0
            else None,
        }
        if (state.rollback_count or 0) > 0 and state.rolled_back_from_sha:
            deploy_event["rolled_back_from_sha"] = state.rolled_back_from_sha[:7]
        events.append(deploy_event)

    # Sort descending by timestamp, nulls last
    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)

    if event_types:
        events = [e for e in events if e["type"] in event_types]

    total = len(events)
    return {"events": events[:limit], "total": total}
