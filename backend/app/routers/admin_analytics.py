"""Activity, digest, and pipeline analytics endpoints."""
import uuid
import json
import logging
from collections import defaultdict
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task, TaskStatus, Project, Artifact, ArtifactType, DeploymentState, DeploymentStatus, _utcnow
)
from app.auth import get_current_user, require_admin
from app.admin import _user_project_ids

logger = logging.getLogger(__name__)

ON_DEMAND_RATE = 0.067 / 3600  # $/sec
SPOT_RATE = 0.020 / 3600  # $/sec

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-analytics"],
    dependencies=[Depends(get_current_user)]
)


def _count_research_findings(logs: Optional[str]) -> int:
    """Parse research findings artifact logs and return finding count."""
    if not logs:
        return 0
    try:
        parsed = json.loads(logs[:100000])
        if isinstance(parsed, list):
            return len(parsed)
        if isinstance(parsed, dict):
            inner = parsed.get("logs", [])
            if isinstance(inner, list):
                return len(inner)
    except (json.JSONDecodeError, AttributeError):
        pass
    return 0


@router.get("/activity")
async def get_activity(
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    event_types: Optional[List[str]] = Query(None),
    since: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Return a reverse-chronological list of recent system events."""
    seven_days_ago = _utcnow() - timedelta(days=7)
    cutoff = since if since is not None else seven_days_ago
    events: List[Dict[str, Any]] = []

    # --- 1 & 2: Completed and Failed tasks in last 7 days (or since cutoff) ---
    user_project_ids = _user_project_ids(current_user["user_id"])
    task_filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= cutoff,
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(
        select(Task).where(*task_filters)
    )
    recent_tasks = tasks_result.scalars().all()

    task_ids = [t.id for t in recent_tasks] if recent_tasks else []

    # Fetch relevant artifacts in one query
    artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> artifact info
    research_artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> research artifact
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error logs
    investigation_report_map: Dict[str, str] = {}  # task_id -> summary text

    if task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                    ArtifactType.RESEARCH_FINDINGS,
                    ArtifactType.INVESTIGATION_REPORT,
                ])
            )
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
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:300]
            elif a.artifact_type == ArtifactType.RESEARCH_FINDINGS:
                research_artifact_map[tid] = {
                    "artifact_created_at": a.created_at,
                    "finding_count": _count_research_findings(a.logs),
                }
            elif a.artifact_type == ArtifactType.INVESTIGATION_REPORT and a.logs:
                investigation_report_map[tid] = a.logs[:2000]

    for t in recent_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        pr_info = artifact_map.get(tid, {})

        # Determine timestamp: last status_log entry or updated_at fallback
        completed_at = None
        if t.status_log:
            last_entry = t.status_log[-1]
            try:
                completed_at = datetime.fromisoformat(last_entry["t"])
            except (KeyError, ValueError):
                pass
        if completed_at is None:
            # fall back to created_at since Task has no updated_at column
            completed_at = t.created_at

        duration_seconds = None
        if completed_at is not None and t.started_at is not None:
            duration_seconds = (completed_at - t.started_at).total_seconds()

        if t.status == TaskStatus.COMPLETED:
            events.append({
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
            })
        elif t.status == TaskStatus.FAILED:
            events.append({
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
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
                "duration_seconds": duration_seconds,
            })

        # --- 3: Research findings events (one per task) ---
        if tid in research_artifact_map:
            ra = research_artifact_map[tid]
            ra_ts = ra["artifact_created_at"]
            events.append({
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
                "finding_count": ra["finding_count"],
                "investigation_summary": investigation_report_map.get(tid),
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
            })

    # --- 3b: In-progress tasks (QUEUED / PROVISIONING / IN_PROGRESS) ---
    inprogress_filters = [
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        inprogress_filters.append(Task.project_id == project_id)
    if since is not None:
        inprogress_filters.append(Task.created_at >= since)

    inprogress_result = await session.execute(
        select(Task).where(*inprogress_filters)
    )
    inprogress_tasks = inprogress_result.scalars().all()

    for t in inprogress_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        events.append({
            "id": f"inprogress-{tid}",
            "type": "TASK_IN_PROGRESS",
            "timestamp": t.started_at.isoformat() if t.started_at else t.created_at.isoformat(),
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
        })

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
            "rollback_count": state.rollback_count if (state.rollback_count or 0) > 0 else None,
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


async def compute_pipeline_health_summary(session: AsyncSession, project_id: uuid.UUID) -> Dict[str, Any]:
    """Compute lightweight pipeline health metrics for the last 24 hours.

    Returns a dict with:
    - success_rate: float or None
    - trend: 'improving' | 'stable' | 'declining' | None
    - failure_distribution: {failure_class: count} top 3
    - direction_stats: [{direction_id, merge_rate, cycles, possibly_saturated}]
    """
    from app.failure_classification import classify_failure as _classify_failure

    _now = _utcnow()
    window_start = _now - timedelta(hours=24)
    prev_window_start = window_start - timedelta(hours=24)

    def _derive_completed_at(t: Task):
        if t.status_log:
            try:
                return datetime.fromisoformat(t.status_log[-1]["t"])
            except (KeyError, ValueError):
                pass
        return t.created_at

    # Query tasks from extended 48h window to compute both current and previous period
    tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= prev_window_start,
        )
    )
    all_tasks = tasks_result.scalars().all()

    # Post-filter by completion timestamp
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # Previous window for trend comparison
    prev_completed = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total = len(prev_completed) + len(prev_failed)
    prev_success_rate = round(len(prev_completed) / prev_total, 2) if prev_total > 0 else None

    trend: Optional[str] = None
    if success_rate is not None and prev_success_rate is not None:
        delta = success_rate - prev_success_rate
        if delta > 0.05:
            trend = "improving"
        elif delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"

    # Failure distribution (top 3 by count)
    failure_counts: Dict[str, int] = {}
    for t in failed_tasks:
        cls = _classify_failure(t)
        failure_counts[cls] = failure_counts.get(cls, 0) + 1
    failure_distribution = dict(
        sorted(failure_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    )

    # Direction stats: merge_rate and cycle count per active direction
    direction_stats: List[Dict[str, Any]] = []
    project = await session.get(Project, project_id)
    if project:
        active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

        dir_tasks: Dict[str, list] = {}
        for t in all_tasks:
            did = (t.context or {}).get("direction_id")
            if did:
                dir_tasks.setdefault(str(did), []).append(t)

        for d in active_directions:
            did = str(d.get("id") or d.get("direction_id") or "")
            if not did:
                continue

            tasks_for_dir = dir_tasks.get(did, [])
            cycles = sum(
                1 for t in tasks_for_dir
                if t.status == TaskStatus.COMPLETED
                and (t.first_payload).get("type", "create") == "research"
                and _derive_completed_at(t) >= window_start
            )

            if cycles == 0:
                continue

            prs_merged = 0
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                except (ValueError, TypeError):
                    pass

            direction_stats.append({
                "direction_id": did,
                "merge_rate": round(prs_merged / cycles, 2),
                "cycles": cycles,
                "possibly_saturated": bool(d.get("possibly_saturated", False)),
            })

        direction_stats.sort(key=lambda x: x["cycles"], reverse=True)

    # First-attempt success rate: tasks that completed on the first try
    terminal_tasks_24h = completed_tasks + failed_tasks
    by_type: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)

    def _first_attempt_rate(tasks):
        if not tasks:
            return None
        first_success = sum(
            1 for t in tasks
            if (t.context or {}).get("_first_attempt_succeeded") is True
        )
        # Only count tasks that have the metric recorded
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(first_success / len(measured), 2)

    tasks_with_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    first_attempt_by_type = {}
    for task_type, tasks in by_type.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_type[task_type] = rate

    current_first_attempt_rate = _first_attempt_rate(terminal_tasks_24h)

    # Previous window for first_attempt trend comparison
    prev_terminal_tasks = prev_completed + prev_failed
    prev_first_attempt_rate = _first_attempt_rate(prev_terminal_tasks)

    first_attempt_trend: Optional[str] = None
    if current_first_attempt_rate is not None and prev_first_attempt_rate is not None:
        fa_delta = current_first_attempt_rate - prev_first_attempt_rate
        if fa_delta > 0.05:
            first_attempt_trend = "improving"
        elif fa_delta < -0.05:
            first_attempt_trend = "declining"
        else:
            first_attempt_trend = "stable"

    first_attempt_success_rate = {
        "overall": current_first_attempt_rate,
        "trend": first_attempt_trend,
        "with_playbook": _first_attempt_rate(tasks_with_playbook) if tasks_with_playbook else None,
        "without_playbook": _first_attempt_rate(tasks_without_playbook) if tasks_without_playbook else None,
        "sample_size": len([t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]),
        "by_type": first_attempt_by_type,
    }

    return {
        "success_rate": success_rate,
        "trend": trend,
        "failure_distribution": failure_distribution,
        "direction_stats": direction_stats,
        "first_attempt_success_rate": first_attempt_success_rate,
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Return a catch-up digest of activity within the requested time window."""
    window_start = _utcnow() - timedelta(hours=hours)
    prev_window_start = window_start - timedelta(hours=hours)
    # Extend lookback to capture long-running tasks and 7 periods for trend history
    raw_lookback = _utcnow() - timedelta(hours=min(hours * 7, 24 * 14))
    lookback_start = min(raw_lookback, prev_window_start)

    def _derive_completed_at(t: Task):
        if t.status_log:
            try:
                return datetime.fromisoformat(t.status_log[-1]["t"])
            except (KeyError, ValueError):
                pass
        return t.created_at

    # --- Query tasks from extended lookback window ---
    task_filters = [Task.created_at >= lookback_start]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(select(Task).where(*task_filters))
    all_tasks = tasks_result.scalars().all()

    # Post-filter completed/failed by completion timestamp, not creation time
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]
    # Approval tasks use created_at (they haven't completed yet)
    approval_tasks = [
        t for t in all_tasks
        if t.requires_approval
        and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and t.created_at >= window_start
    ]

    # --- Query active tasks (not filtered by window — they may have started before it) ---
    _active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [Task.status.in_(_active_statuses)]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_result = await session.execute(
        select(Task).where(*active_filters).order_by(Task.started_at.asc().nullsfirst())
    )
    active_tasks_all = active_result.scalars().all()

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1 for t in completed_tasks
        if (t.first_payload).get("type", "create") == "research"
    )

    # --- Direction progress (only when project_id provided) ---
    direction_progress_list: List[Dict[str, Any]] = []
    dir_id_to_text: Dict[str, str] = {}

    if project_id:
        project = await session.get(Project, project_id)
        if project:
            active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

            # Build direction_id -> text lookup
            for d in active_directions:
                did = d.get("id") or d.get("direction_id")
                if did:
                    dir_id_to_text[str(did)] = d.get("text") or d.get("description") or ""

            # Build direction_id -> tasks mapping
            dir_tasks: Dict[str, List] = {}
            for t in all_tasks:
                did = (t.context or {}).get("direction_id")
                if did:
                    dir_tasks.setdefault(str(did), []).append(t)

            for d in active_directions:
                did = str(d.get("id") or d.get("direction_id") or "")
                if not did:
                    continue

                tasks_for_dir = dir_tasks.get(did, [])
                cycles_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.status == TaskStatus.COMPLETED
                    and (t.first_payload).get("type", "create") == "research"
                    and _derive_completed_at(t) >= window_start
                )
                tasks_created_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.created_at >= window_start
                )

                # prs_merged: sum from research_history entries within window
                prs_merged = 0
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                        if entry_ts >= window_start:
                            prs_merged += entry.get("prs_merged") or 0
                    except (ValueError, TypeError):
                        pass

                if cycles_in_window == 0 and tasks_created_in_window == 0:
                    continue

                last_entry = (d.get("research_history") or [])[-1] if d.get("research_history") else None
                latest_insights = last_entry.get("cycle_insights", []) if last_entry else []
                recent_finding_titles = last_entry.get("finding_titles", []) if last_entry else []
                latest_research_at = last_entry.get("timestamp") if last_entry else None

                # Compute product_ratio and type_distribution from window entries
                window_product_ratios = []
                agg_type_distribution: Dict[str, int] = {}
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                        if entry_ts >= window_start:
                            pr = entry.get("product_ratio")
                            if pr is not None:
                                window_product_ratios.append(float(pr))
                            for ttype, cnt in (entry.get("type_distribution") or {}).items():
                                agg_type_distribution[ttype] = agg_type_distribution.get(ttype, 0) + cnt
                    except (ValueError, TypeError):
                        pass
                dir_product_ratio = round(sum(window_product_ratios) / len(window_product_ratios), 3) if window_product_ratios else None

                dir_cost = sum((t.vm_spot and SPOT_RATE or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in tasks_for_dir if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start)

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                direction_progress_list.append({
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                    "product_ratio": dir_product_ratio,
                    "type_distribution": agg_type_distribution or None,
                    "estimated_cost_usd": round(dir_cost, 2),
                    "cost_per_merged_pr": round(dir_cost / prs_merged, 4) if prs_merged > 0 else None,
                })

            direction_progress_list.sort(key=lambda x: x["cycles_in_window"], reverse=True)

    # --- Batch artifact query ---
    all_task_ids = [t.id for t in all_tasks]
    pr_count = 0
    pr_artifact_map: Dict[str, str] = {}   # task_id -> pr_url
    error_log_map: Dict[str, str] = {}     # task_id -> truncated error snippet

    if all_task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(all_task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                if a.created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = a.pr_url or ""
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:500]

    # --- needs_attention: failed + pending approval ---
    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "reason": "failed",
            "error_snippet": error_log_map.get(tid),
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
        })
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Pending approval",
            "reason": "approval_required",
            "error_snippet": None,
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
            "approval_reason": t.latest_status_message,
            "touched_critical": (t.context or {}).get("touched_critical", []),
        })
    needs_attention.sort(key=lambda x: (0 if x["reason"] == "approval_required" else 1, -(x["waiting_hours"] or 0)))

    # --- recent_failures (up to 10, desc) ---
    sorted_failures = sorted(failed_tasks, key=_derive_completed_at, reverse=True)
    recent_failures = []
    for t in sorted_failures[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        recent_failures.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "failed_at": ts.isoformat() if ts else None,
            "error_snippet": error_log_map.get(tid),
            "waiting_hours": waiting_hours,
            "attempt_count": t.attempt_count,
        })

    # --- failure_groups (classify failures by type) ---
    from app.failure_classification import classify_failure as _classify_failure
    _failure_class_labels = {
        "CODE_ERROR": "Code Error",
        "PROVISION_FAILURE": "Provision Failure",
        "AGENT_TIMEOUT": "Agent Timeout",
        "EXTERNAL_API": "External API",
        "RATE_LIMIT": "Rate Limit",
    }
    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in failed_tasks:
        tid = str(t.id)
        cls = _classify_failure(t, error_log_map.get(tid))
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        _failure_class_groups.setdefault(cls, []).append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "error_snippet": error_log_map.get(tid),
            "attempt_count": t.attempt_count,
            "waiting_hours": waiting_hours,
        })
    failure_groups = sorted(
        [
            {
                "failure_class": cls,
                "label": _failure_class_labels.get(cls, cls),
                "count": len(tasks),
                "tasks": tasks,
            }
            for cls, tasks in _failure_class_groups.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # --- recent_completions (up to 10, desc) ---
    sorted_completions = sorted(completed_tasks, key=_derive_completed_at, reverse=True)
    recent_completions = []
    for t in sorted_completions[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        task_type = (t.first_payload).get("type", "create")
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        duration_seconds = None
        if t.started_at and ts:
            duration_seconds = int((ts - t.started_at).total_seconds())
        recent_completions.append({
            "task_id": tid,
            "title": t.title or "Completed task",
            "completed_at": ts.isoformat() if ts else None,
            "pr_url": pr_artifact_map.get(tid),
            "task_type": task_type,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
            "duration_seconds": duration_seconds,
        })

    # --- active_tasks list (up to 5, oldest started first) ---
    active_tasks_list = [
        {
            "task_id": str(t.id),
            "title": t.title or "Active task",
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }
        for t in active_tasks_all[:5]
    ]

    running_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING}
    queued_statuses = {TaskStatus.QUEUED, TaskStatus.EVALUATING}
    running_count = sum(1 for t in active_tasks_all if t.status in running_statuses)
    queued_count = sum(1 for t in active_tasks_all if t.status in queued_statuses)

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # --- narrative_summary ---
    completed_count = len(completed_tasks)
    failed_count = len(failed_tasks)
    pending_approval_count = len(approval_tasks)

    # --- Previous window metrics for trend comparison ---
    prev_completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total_terminal = len(prev_completed_tasks) + len(prev_failed_tasks)
    prev_success_rate = round(len(prev_completed_tasks) / prev_total_terminal, 2) if prev_total_terminal > 0 else None
    prev_completed_count = len(prev_completed_tasks)
    prev_failed_count = len(prev_failed_tasks)

    completed_delta = completed_count - prev_completed_count
    failed_delta = failed_count - prev_failed_count
    success_rate_delta: Optional[float] = (
        round(success_rate - prev_success_rate, 2)
        if success_rate is not None and prev_success_rate is not None
        else None
    )
    if success_rate_delta is not None:
        if success_rate_delta > 0.05:
            trend: Optional[str] = "improving"
        elif success_rate_delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = None

    # Compute trend history: 7 periods (6 previous + current), oldest first
    _now = _utcnow()
    trend_history = []
    for _i in range(7):
        _p_start = _now - timedelta(hours=hours * (7 - _i))
        _p_end = _now - timedelta(hours=hours * (6 - _i))
        _p_completed = sum(
            1 for t in all_tasks
            if t.status == TaskStatus.COMPLETED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_failed_tasks = [
            t for t in all_tasks
            if t.status == TaskStatus.FAILED
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_failed = len(_p_failed_tasks)
        _p_by_class: dict[str, int] = {}
        for _ft in _p_failed_tasks:
            _cls = _classify_failure(_ft)
            _p_by_class[_cls] = _p_by_class.get(_cls, 0) + 1
        _p_total = _p_completed + _p_failed
        _p_cost = sum((t.vm_spot and SPOT_RATE or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and _p_start <= t.vm_stopped_at < _p_end)
        trend_history.append({
            "period_end": _p_end.isoformat(),
            "success_rate": round(_p_completed / _p_total, 2) if _p_total > 0 else None,
            "completed": _p_completed,
            "failed": _p_failed,
            "failed_by_class": _p_by_class,
            "period_cost_usd": round(_p_cost, 4),
        })

    # Action-first narrative: executives want "what do I need to do?" before "how did things go?"
    narrative_parts = []

    # All clear shortcut: nothing needs attention and there are completions
    if pending_approval_count == 0 and failed_count == 0 and completed_count > 0:
        all_clear = f"All clear — {completed_count} task{'s' if completed_count != 1 else ''} completed"
        if pr_count > 0:
            all_clear += f" with {pr_count} PR{'s' if pr_count != 1 else ''} merged"
        all_clear += ", no action needed."
        narrative_summary = all_clear
    else:
        # FIRST: Approval items — most actionable, lead with them
        if pending_approval_count > 0:
            approval_items = [
                x for x in needs_attention if x["reason"] == "approval_required"
            ]
            max_wait = max((x.get("waiting_hours") or 0 for x in approval_items), default=0)
            approval_str = f"{pending_approval_count} task{'s' if pending_approval_count != 1 else ''} awaiting your approval"
            if max_wait > 0:
                approval_str += f" ({max_wait}h)"
            narrative_parts.append(approval_str + ".")

        # SECOND: Failures with root cause
        if failed_count > 0:
            dominant_group = failure_groups[0] if failure_groups else None
            dominant_msg = None
            if dominant_group and dominant_group["count"] >= 2 and dominant_group["count"] / failed_count >= 0.6:
                cls = dominant_group["failure_class"]
                cnt = dominant_group["count"]
                _root_cause_msgs = {
                    "RATE_LIMIT": f"Most failures ({cnt}/{failed_count}) are rate-limit errors — consider pausing until account recovery.",
                    "PROVISION_FAILURE": f"Most failures ({cnt}/{failed_count}) are provisioning issues — check GCP quota and zone health.",
                    "EXTERNAL_API": f"Most failures ({cnt}/{failed_count}) are external API errors — GitHub/Claude API may be experiencing issues.",
                    "AGENT_TIMEOUT": f"Most failures ({cnt}/{failed_count}) are agent timeouts — tasks may need complexity reduction.",
                    "CODE_ERROR": f"Most failures ({cnt}/{failed_count}) are code errors in agent-written code.",
                }
                dominant_msg = _root_cause_msgs.get(cls)
            if dominant_msg:
                narrative_parts.append(dominant_msg)
            else:
                failure_titles = [t.title or "Failed task" for t in sorted_failures[:2]]
                failed_str = f"{failed_count} task{'s' if failed_count != 1 else ''} failed"
                if failure_titles:
                    failed_str += f" ({', '.join(failure_titles)})"
                narrative_parts.append(failed_str + ".")

        # THIRD: Completion stats with trend prefix embedded
        if completed_count > 0:
            trend_prefix = ""
            if trend == "declining":
                trend_prefix = "Pipeline health declining — "
            elif trend == "improving":
                trend_prefix = "Good progress — "
            shipped = f"{trend_prefix}{completed_count} task{'s' if completed_count != 1 else ''} completed"
            if pr_count > 0:
                shipped += f" with {pr_count} PR{'s' if pr_count != 1 else ''} opened"
            completion_titles = [t.title or "Completed task" for t in sorted_completions[:2]]
            completion_titles = [t[:50] + "..." if len(t) > 50 else t for t in completion_titles]
            if completion_titles:
                shipped += f" including {', '.join(completion_titles)}"
            if completed_delta > 0:
                shipped += f" (up from {prev_completed_count} last period)"
            elif completed_delta < 0:
                shipped += f" (down from {prev_completed_count} last period)"
            narrative_parts.append(shipped + ".")

        # FOURTH: Direction progress
        if project_id and direction_progress_list:
            dir_summaries = []
            for dp in direction_progress_list:
                dt = (dp["direction_text"] or "")[:40]
                if dp["prs_merged"] > 0:
                    dir_summaries.append(f"{dt}: {dp['prs_merged']} PR{'s' if dp['prs_merged'] != 1 else ''} merged")
                elif dp.get("possibly_saturated"):
                    dir_summaries.append(f"{dt}: may be saturated")
                elif dp["tasks_created"] > 0:
                    dir_summaries.append(f"{dt}: {dp['tasks_created']} task{'s' if dp['tasks_created'] != 1 else ''} in progress")
            if dir_summaries:
                narrative_parts.append("Research: " + "; ".join(dir_summaries) + ".")

        # FIFTH: Pipeline status
        if running_count > 0 or queued_count > 0:
            pipeline_parts = []
            if running_count > 0:
                pipeline_parts.append(f"{running_count} running")
            if queued_count > 0:
                pipeline_parts.append(f"{queued_count} queued")
            narrative_parts.append(", ".join(pipeline_parts) + ".")

        if narrative_parts:
            narrative_summary = " ".join(narrative_parts)
        else:
            narrative_summary = f"All quiet — no activity in the last {hours}h."

    # Compute total estimated cost (directed + undirected tasks)
    directed_task_ids = {t.id for t in all_tasks if (t.context or {}).get("direction_id")}
    undirected_cost = sum((t.vm_spot and SPOT_RATE or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start and t.id not in directed_task_ids)
    total_estimated_cost_usd = round(sum(d["estimated_cost_usd"] for d in direction_progress_list) + undirected_cost, 2)

    # Compute overall product_ratio and type_distribution across all directions
    all_product_ratios = [d["product_ratio"] for d in direction_progress_list if d.get("product_ratio") is not None]
    overall_product_ratio = round(sum(all_product_ratios) / len(all_product_ratios), 3) if all_product_ratios else None
    overall_type_distribution: Dict[str, int] = {}
    for d in direction_progress_list:
        for ttype, cnt in (d.get("type_distribution") or {}).items():
            overall_type_distribution[ttype] = overall_type_distribution.get(ttype, 0) + cnt

    # --- Latest strategic cycle ---
    latest_cycle = None
    if project_id:
        _proj = await session.get(Project, project_id)
        if _proj:
            cycle_history = (_proj.vision or {}).get("_strategic_cycle_history", [])
            completed_cycles = [c for c in cycle_history if c.get("outcome") not in (None, "in_progress")]
            if completed_cycles:
                last = max(completed_cycles, key=lambda c: c.get("started_at") or "")
                latest_cycle = {
                    "cycle_id": last.get("cycle_id"),
                    "started_at": last.get("started_at"),
                    "completed_at": last.get("phase2_completed_at"),
                    "outcome": last.get("outcome"),
                    "directions_created": last.get("directions_created", 0),
                    "directions_retired": last.get("directions_retired", 0),
                    "vision_updated": last.get("vision_updated", False),
                    "summary_text": last.get("summary_text"),
                }

    return {
        "window_hours": hours,
        "generated_at": _utcnow().isoformat(),
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "pending_approval_count": len(approval_tasks),
        "research_cycles": research_cycles,
        "prs_opened": pr_count,
        "active_count": len(active_tasks_all),
        "running_count": running_count,
        "queued_count": queued_count,
        "active_tasks": active_tasks_list,
        "success_rate": success_rate,
        "needs_attention": needs_attention,
        "recent_failures": recent_failures,
        "failure_groups": failure_groups,
        "recent_completions": recent_completions,
        "direction_progress": direction_progress_list,
        "overall_product_ratio": overall_product_ratio,
        "overall_type_distribution": overall_type_distribution or None,
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "narrative_summary": narrative_summary,
        "prev_completed_count": prev_completed_count,
        "prev_failed_count": prev_failed_count,
        "prev_success_rate": prev_success_rate,
        "completed_delta": completed_delta,
        "failed_delta": failed_delta,
        "success_rate_delta": success_rate_delta,
        "trend": trend,
        "trend_history": trend_history,
        "latest_cycle": latest_cycle,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/health-overview
# ---------------------------------------------------------------------------

@router.get("/health-overview")
async def get_health_overview(
    project_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return a unified health overview with top actionable issues."""
    from app.shared_state import external_health, circuit_breakers, get_circuit_breaker
    from app.compute import get_zone_health_summary
    from app.routers.admin_settings import _build_stuck_tasks  # noqa: PLC0415

    now = _utcnow()
    actionable_issues: List[Dict[str, Any]] = []

    # --- 1. Success rate ---
    if project_id:
        health_summary = await compute_pipeline_health_summary(session, project_id)
        success_rate_value = health_summary.get("success_rate")
        sr_trend = health_summary.get("trend")
    else:
        window_start = now - timedelta(hours=24)
        prev_window_start = window_start - timedelta(hours=24)
        xp_result = await session.execute(
            select(Task).where(
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.created_at >= prev_window_start,
                Task.archived.is_(False),
            )
        )
        xp_tasks = xp_result.scalars().all()

        def _derive_at(t: Task):
            if t.status_log:
                try:
                    return datetime.fromisoformat(t.status_log[-1]["t"])
                except (KeyError, ValueError):
                    pass
            return t.created_at

        xp_completed = [t for t in xp_tasks if t.status == TaskStatus.COMPLETED and _derive_at(t) >= window_start]
        xp_failed = [t for t in xp_tasks if t.status == TaskStatus.FAILED and _derive_at(t) >= window_start]
        xp_total = len(xp_completed) + len(xp_failed)
        success_rate_value = round(len(xp_completed) / xp_total, 2) if xp_total > 0 else None

        prev_completed = [t for t in xp_tasks if t.status == TaskStatus.COMPLETED and prev_window_start <= _derive_at(t) < window_start]
        prev_failed = [t for t in xp_tasks if t.status == TaskStatus.FAILED and prev_window_start <= _derive_at(t) < window_start]
        prev_total = len(prev_completed) + len(prev_failed)
        prev_sr = round(len(prev_completed) / prev_total, 2) if prev_total > 0 else None
        sr_trend: Optional[str] = None
        if success_rate_value is not None and prev_sr is not None:
            delta = success_rate_value - prev_sr
            if delta > 0.05:
                sr_trend = "improving"
            elif delta < -0.05:
                sr_trend = "declining"
            else:
                sr_trend = "stable"

    success_rate_out = {
        "current": success_rate_value,
        "trend": sr_trend,
        "window_hours": 24,
    }

    # --- 2. Active tasks for stuck-task detection and queue depth ---
    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [Task.status.in_(active_statuses), Task.archived.is_(False)]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_result = await session.execute(select(Task).where(*active_filters))
    active_tasks = active_result.scalars().all()

    # Zone health (needed for remediation hints)
    try:
        zone_summary = get_zone_health_summary()
    except Exception:
        zone_summary = {"available": 0, "exhausted": []}

    stuck_tasks = await _build_stuck_tasks(active_tasks, now, session)

    # Group by reason and emit one issue per reason group
    stuck_by_reason: Dict[str, List[Dict]] = {}
    for st in stuck_tasks:
        stuck_by_reason.setdefault(st["stuck_reason"], []).append(st)

    for reason, s_tasks in stuck_by_reason.items():
        task_ids = [t["task_id"] for t in s_tasks]
        count = len(s_tasks)
        exhausted = zone_summary.get("exhausted", [])
        available = zone_summary.get("available", 0)

        if reason == "provision_timeout":
            if exhausted:
                hint = f"Zone(s) {', '.join(exhausted)} exhausted. Tasks retry when zones recover."
            else:
                hint = "VM provisioning timed out. Tasks will be reaped and retried automatically."
            actionable_issues.append({
                "severity": "warning",
                "title": f"{count} task(s) stuck on VM provisioning",
                "hint": hint,
                "task_ids": task_ids,
                "category": "stuck_tasks",
            })
        elif reason == "no_heartbeat":
            actionable_issues.append({
                "severity": "warning",
                "title": f"{count} task(s) lost heartbeat",
                "hint": "The reaper will reclaim and retry these tasks automatically.",
                "task_ids": task_ids,
                "category": "stuck_tasks",
            })
        elif reason == "long_queued":
            if available == 0:
                hint = "All zones exhausted — no capacity available. Check zone health."
            else:
                hint = "Tasks queued >30 min. Check whether max_concurrent_vms limit is reached."
            actionable_issues.append({
                "severity": "warning",
                "title": f"{count} task(s) long-queued (>30 min)",
                "hint": hint,
                "task_ids": task_ids,
                "category": "stuck_tasks",
            })
        elif reason == "stuck_deps":
            all_dep_ids: List[str] = []
            for t in s_tasks:
                all_dep_ids.extend(t.get("unresolvable_deps", []))
            unique_deps = list(dict.fromkeys(all_dep_ids))
            dep_str = ", ".join(unique_deps[:5]) + ("..." if len(unique_deps) > 5 else "")
            actionable_issues.append({
                "severity": "warning",
                "title": f"{count} task(s) blocked by failed dependencies",
                "hint": f"Blocking dep(s): {dep_str}. Cancel or requeue the blocking tasks.",
                "task_ids": task_ids,
                "category": "stuck_tasks",
            })
        elif reason == "evaluation_timeout":
            actionable_issues.append({
                "severity": "warning",
                "title": f"{count} task(s) stuck in evaluation (>2 h)",
                "hint": "Evaluation exceeded the 2-hour threshold. Consider manual review or cancellation.",
                "task_ids": task_ids,
                "category": "stuck_tasks",
            })

    # --- 3. Circuit breaker ---
    if project_id:
        cb = get_circuit_breaker(str(project_id))
        if cb["state"] == "OPEN":
            actionable_issues.append({
                "severity": "critical",
                "title": "Circuit breaker OPEN — autonomous dispatch halted",
                "hint": f"Too many consecutive failures tripped the breaker. Auto-recovers after {cb.get('cooldown_seconds', 1800) // 60} min cooldown.",
                "task_ids": [],
                "category": "circuit_breaker",
            })
        elif cb["state"] == "HALF_OPEN":
            actionable_issues.append({
                "severity": "warning",
                "title": "Circuit breaker HALF_OPEN — testing recovery",
                "hint": "One test task is running to verify recovery. Monitor for success.",
                "task_ids": [],
                "category": "circuit_breaker",
            })
    else:
        for proj_id, cb in circuit_breakers.items():
            if cb["state"] == "OPEN":
                actionable_issues.append({
                    "severity": "critical",
                    "title": f"Circuit breaker OPEN for project {proj_id[:8]}",
                    "hint": f"Auto-recovers after {cb.get('cooldown_seconds', 1800) // 60} min cooldown.",
                    "task_ids": [],
                    "category": "circuit_breaker",
                })
            elif cb["state"] == "HALF_OPEN":
                actionable_issues.append({
                    "severity": "warning",
                    "title": f"Circuit breaker HALF_OPEN for project {proj_id[:8]}",
                    "hint": "One test task running to verify recovery.",
                    "task_ids": [],
                    "category": "circuit_breaker",
                })

    # --- 4. Zone health ---
    exhausted_zones = zone_summary.get("exhausted", [])
    available_zones = zone_summary.get("available", 0)
    total_zones = available_zones + len(exhausted_zones)
    if total_zones > 0 and len(exhausted_zones) > total_zones / 2:
        actionable_issues.append({
            "severity": "warning",
            "title": f"{len(exhausted_zones)}/{total_zones} zones exhausted",
            "hint": f"Exhausted: {', '.join(exhausted_zones)}. New task dispatch may be delayed.",
            "task_ids": [],
            "category": "zone_exhaustion",
        })

    # --- 5. External dependency health ---
    failed_deps = external_health.get("failed_deps", [])
    if failed_deps:
        actionable_issues.append({
            "severity": "critical",
            "title": f"External dependency failure: {', '.join(failed_deps)}",
            "hint": "One or more external services unreachable. Check service status dashboards.",
            "task_ids": [],
            "category": "external_dep",
        })

    # --- 6. Deploy state ---
    deploy_state = await session.get(DeploymentState, 1)
    if deploy_state:
        if deploy_state.status == DeploymentStatus.MONITORING:
            sha_short = deploy_state.current_sha[:7] if deploy_state.current_sha else "unknown"
            actionable_issues.append({
                "severity": "warning",
                "title": f"Deploy {sha_short} in monitoring window",
                "hint": "Current deploy is being monitored for stability. Avoid config changes.",
                "task_ids": [],
                "category": "deploy_state",
            })
        elif deploy_state.status in (DeploymentStatus.PAUSED, DeploymentStatus.ROLLED_BACK, DeploymentStatus.HALTED):
            reason_msg = deploy_state.pause_reason or deploy_state.rollback_reason or "Manual intervention may be required."
            actionable_issues.append({
                "severity": "warning",
                "title": f"Deploy status: {deploy_state.status}",
                "hint": reason_msg,
                "task_ids": [],
                "category": "deploy_state",
            })

    # Sort by severity (critical first) and return top 3
    _sev_order = {"critical": 0, "warning": 1, "info": 2}
    actionable_issues.sort(key=lambda x: _sev_order.get(x["severity"], 2))
    actionable_issues = actionable_issues[:3]

    # --- 7. Overall status ---
    has_critical = any(i["severity"] == "critical" for i in actionable_issues)
    has_warning = any(i["severity"] == "warning" for i in actionable_issues)
    if has_critical:
        overall_status = "unhealthy"
    elif has_warning:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    # --- 8. Direction portfolio ---
    direction_portfolio: Dict[str, int] = {
        "product": 0,
        "structural": 0,
        "mixed": 0,
        "uncategorized": 0,
        "total_active": 0,
    }
    if project_id:
        project = await session.get(Project, project_id)
        projects_for_dirs = [project] if project else []
    else:
        all_projects_result = await session.execute(select(Project))
        projects_for_dirs = all_projects_result.scalars().all()

    for proj in projects_for_dirs:
        active_dirs = [
            d for d in (proj.directions or [])
            if d.get("status") not in ("retired", "paused")
        ]
        for d in active_dirs:
            cat = d.get("category")
            if cat in ("product", "structural", "mixed"):
                direction_portfolio[cat] += 1
            else:
                direction_portfolio["uncategorized"] += 1
        direction_portfolio["total_active"] += len(active_dirs)

    # --- 9. Queue depth ---
    queue_depth = {
        "queued": sum(1 for t in active_tasks if t.status == TaskStatus.QUEUED),
        "active": sum(1 for t in active_tasks if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING)),
        "evaluating": sum(1 for t in active_tasks if t.status == TaskStatus.EVALUATING),
    }

    return {
        "overall_status": overall_status,
        "success_rate": success_rate_out,
        "actionable_issues": actionable_issues,
        "direction_portfolio": direction_portfolio,
        "queue_depth": queue_depth,
    }

@router.get("/task-duration-stats")
async def get_task_duration_stats(
    project_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """p50 duration per task type from COMPLETED tasks in last 7 days."""
    filters = [Task.status == TaskStatus.COMPLETED, Task.started_at.is_not(None), Task.created_at >= _utcnow() - timedelta(days=7)]
    if project_id:
        filters.append(Task.project_id == project_id)
    completed = (await session.execute(select(Task).where(*filters))).scalars().all()
    type_durations: dict = defaultdict(list)
    for t in completed:
        try: cat = datetime.fromisoformat(t.status_log[-1]["t"]) if t.status_log else t.created_at
        except Exception: cat = t.created_at
        dur = (cat - t.started_at).total_seconds()
        if dur > 0: type_durations[(t.first_payload).get("type", "create")].append(dur)
    stats: dict = {}
    for tt, durs in type_durations.items():
        if len(durs) >= 3:
            sd = sorted(durs); mid = len(sd) // 2
            stats[tt] = {"p50_seconds": sd[mid] if len(sd) % 2 else (sd[mid - 1] + sd[mid]) / 2, "sample_count": len(durs)}
    return {"stats": stats}
