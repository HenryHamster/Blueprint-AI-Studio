"""Activity, digest, and pipeline analytics endpoints."""
import os
import re
import time
import uuid
import json
import logging
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task, TaskStatus, Project, Artifact, ArtifactType, DeploymentState, DeploymentStatus,
    ProjectMessage, OrchestratorErrorAck, _utcnow,
)
from app.auth import get_current_user, require_admin
from app.admin import _user_project_ids
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


def _derive_completed_at(t: "Task") -> datetime:
    """Return the completion timestamp from the last status_log entry, or task.created_at."""
    if t.status_log:
        try:
            return datetime.fromisoformat(t.status_log[-1]["t"])
        except (KeyError, ValueError):
            pass
    return t.created_at


ON_DEMAND_RATE = 0.067 / 3600  # $/sec
SPOT_RATE = 0.020 / 3600  # $/sec

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-analytics"],
    dependencies=[Depends(get_current_user)]
)


def _compute_scope_baselines(tasks: list, lookback_days: int) -> dict:
    """Compute per-scope merge rate baselines from a pre-fetched list of terminal tasks.

    Groups tasks by ISO-week (Monday-keyed) and scope (from context['scope']),
    computing merge_rate = COMPLETED / total terminal per bucket.  Also returns
    an overall summary per scope with trend derived from first-to-last week delta.
    """
    bucket: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: {"created": 0, "merged": 0})

    for t in tasks:
        scope = (t.context or {}).get("scope", "unknown")
        task_date = t.created_at.date() if t.created_at else None
        if task_date is None:
            continue
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        bucket[(week_key, scope)]["created"] += 1
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
            bucket[(week_key, scope)]["merged"] += 1

    # Build time-series sorted by week then scope
    series = []
    for (week, scope), counts in sorted(bucket.items()):
        created = counts["created"]
        merged = counts["merged"]
        merge_rate = round(merged / created, 3) if created > 0 else None
        series.append({
            "week": week,
            "scope": scope,
            "created": created,
            "merged": merged,
            "merge_rate": merge_rate,
        })

    # Build per-scope summary: overall rate + trend from first-to-last week
    all_scopes: set = {scope for (_, scope) in bucket}
    summary: Dict[str, Any] = {}
    for scope in sorted(all_scopes):
        total_created = sum(c["created"] for (_, sc), c in bucket.items() if sc == scope)
        total_merged = sum(c["merged"] for (_, sc), c in bucket.items() if sc == scope)
        overall_rate = round(total_merged / total_created, 3) if total_created > 0 else None

        scope_weeks = sorted((wk, c) for (wk, sc), c in bucket.items() if sc == scope)
        scope_trend: str | None = None
        if len(scope_weeks) >= 2:
            first_c = scope_weeks[0][1]
            last_c = scope_weeks[-1][1]
            first_rate = first_c["merged"] / first_c["created"] if first_c["created"] > 0 else None
            last_rate = last_c["merged"] / last_c["created"] if last_c["created"] > 0 else None
            if first_rate is not None and last_rate is not None:
                delta = last_rate - first_rate
                if delta > 0.05:
                    scope_trend = "improving"
                elif delta < -0.05:
                    scope_trend = "declining"
                else:
                    scope_trend = "stable"

        summary[scope] = {
            "merge_rate": overall_rate,
            "created": total_created,
            "merged": total_merged,
            "trend": scope_trend,
        }

    return {
        "series": series,
        "summary": summary,
        "lookback_days": lookback_days,
    }


def _count_research_findings(logs: str | None) -> int:
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
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    event_types: List[str] | None = Query(None),
    since: datetime | None = Query(None),
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
        select(Task).where(*task_filters).options(
            load_only(Task.id, Task.status, Task.title, Task.project_id,
                      Task.payloads, Task.status_log, Task.created_at, Task.started_at)
        )
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


async def compute_pipeline_health_summary(session: AsyncSession, project_id: uuid.UUID, lookback_hours: int = 24) -> Dict[str, Any]:
    """Compute lightweight pipeline health metrics for the specified lookback window.

    Returns a dict with:
    - success_rate: float or None
    - trend: 'improving' | 'stable' | 'declining' | None
    - failure_distribution: {failure_class: count} top 3
    - direction_stats: [{direction_id, merge_rate, cycles, possibly_saturated}]
    """
    from app.failure_classification import classify_failure as _classify_failure

    _now = _utcnow()
    window_start = _now - timedelta(hours=lookback_hours)
    prev_window_start = window_start - timedelta(hours=lookback_hours)

    def _safe_ts_in_window(ts_str: str | None, cutoff: datetime) -> bool:
        try:
            return datetime.fromisoformat(ts_str or "") >= cutoff
        except (ValueError, TypeError):
            return False

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

    trend: str | None = None
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

    from app.failure_classification import (
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY, TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
    )
    _taxonomy_keys = (
        TAXONOMY_SCOPE_RELATED, TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED, TAXONOMY_CODE_DEFECT,
    )
    taxonomy_counts: Dict[str, int] = {k: 0 for k in _taxonomy_keys}
    for t in failed_tasks:
        taxonomy_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(taxonomy_counts.values())
    failure_taxonomy = {
        "categories": {
            k: {
                "count": taxonomy_counts[k],
                "percentage": round(taxonomy_counts[k] / _tax_total * 100, 1) if _tax_total else 0.0,
            }
            for k in _taxonomy_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_taxonomy_keys, key=lambda k: taxonomy_counts[k]) if _tax_total else None,
    }

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
            tasks_created_total = 0
            sd_totals: Dict[str, int] = {}
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        tasks_created_total += entry.get("tasks_created") or 0
                        for k, v in (entry.get("stage_dropout") or {}).items():
                            sd_totals[k] = sd_totals.get(k, 0) + (v or 0)
                except (ValueError, TypeError):
                    pass

            dir_conversion_rate = round(prs_merged / tasks_created_total, 3) if tasks_created_total > 0 else None
            dominant_dropout = (
                max(sd_totals, key=lambda k: sd_totals[k])
                if sd_totals and max(sd_totals.values()) > 0
                else None
            )
            direction_stats.append({
                "direction_id": did,
                "merge_rate": round(prs_merged / cycles, 2),
                "cycles": cycles,
                "possibly_saturated": bool(d.get("possibly_saturated", False)),
                "conversion_rate": dir_conversion_rate,
                "dominant_dropout_stage": dominant_dropout,
            })

        direction_stats.sort(key=lambda x: x["cycles"], reverse=True)

    # Pipeline-level conversion rate: total prs_merged / total tasks_created across all directions
    _pipeline_total_created = sum(
        entry.get("tasks_created") or 0
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    _pipeline_total_merged = sum(
        entry.get("prs_merged") or 0
        for d in (project.directions or [] if project else [])
        for entry in (d.get("research_history") or [])
        if _safe_ts_in_window(entry.get("timestamp"), window_start)
    )
    pipeline_conversion_rate = round(_pipeline_total_merged / _pipeline_total_created, 3) if _pipeline_total_created > 0 else None

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

    _review_eligible_types = {"create", "refactor"}

    def _first_review_approval_rate(tasks):
        """Fraction of review-eligible merged tasks that had no first-review rejection.

        A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
        A task 'approved on first review' if it does NOT have _first_attempt_rejection_categories
        in its context (meaning the first review returned APPROVE, not REQUEST_CHANGES).
        """
        eligible = [
            t for t in tasks
            if (t.first_payload or {}).get("type", "create") in _review_eligible_types
            and (t.context or {}).get("pr_merged") is True
        ]
        if not eligible:
            return None
        approved = sum(
            1 for t in eligible
            if "_first_attempt_rejection_categories" not in (t.context or {})
        )
        return round(approved / len(eligible), 2)

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

    _arch_eligible = [t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]
    tasks_with_arch_context = [t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is True]
    tasks_without_arch_context = [t for t in _arch_eligible if (t.context or {}).get("_had_arch_context") is False]

    tasks_with_review_measurements = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_with_measurements = tasks_with_review_measurements
    tasks_without_review_measurements = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_review_measurements") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_measurements = [
        t for t in terminal_tasks_24h
        if not (t.context or {}).get("_had_review_measurements")
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    first_attempt_by_type = {}
    for task_type, tasks in by_type.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_type[task_type] = rate

    by_scope: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        scope = (t.context or {}).get("scope", "unknown")
        by_scope.setdefault(scope, []).append(t)

    first_attempt_by_scope = {}
    for scope, tasks in by_scope.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_scope[scope] = rate

    # Scope merge rates: per-scope merged/created from research_history scope_breakdown
    _scope_cur: Dict[str, Dict[str, int]] = {}
    _scope_prev: Dict[str, Dict[str, int]] = {}
    for d in (project.directions or [] if project else []):
        for entry in (d.get("research_history") or []):
            ts_str = entry.get("timestamp")
            sb = entry.get("scope_breakdown") or {}
            if not sb:
                continue
            in_cur = _safe_ts_in_window(ts_str, window_start)
            in_prev = (
                not in_cur
                and _safe_ts_in_window(ts_str, prev_window_start)
            )
            target = _scope_cur if in_cur else (_scope_prev if in_prev else None)
            if target is None:
                continue
            for scope, counts in sb.items():
                if scope not in target:
                    target[scope] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                target[scope]["created"] += counts.get("created") or 0
                target[scope]["merged"] += counts.get("merged") or 0
                target[scope]["calibrated_in"] += counts.get("calibrated_in") or 0
                target[scope]["calibrated_out"] += counts.get("calibrated_out") or 0

    all_scopes = set(_scope_cur) | set(_scope_prev)
    scope_merge_rates: Dict[str, Any] = {}
    for scope in all_scopes:
        cur = _scope_cur.get(scope, {})
        prev = _scope_prev.get(scope, {})
        cur_created = cur.get("created", 0)
        prev_created = prev.get("created", 0)
        cur_rate = round(cur.get("merged", 0) / cur_created, 3) if cur_created > 0 else None
        prev_rate = round(prev.get("merged", 0) / prev_created, 3) if prev_created > 0 else None
        delta = round(cur_rate - prev_rate, 3) if (cur_rate is not None and prev_rate is not None) else None
        scope_trend: str | None = None
        if delta is not None:
            if delta > 0.05:
                scope_trend = "improving"
            elif delta < -0.05:
                scope_trend = "declining"
            else:
                scope_trend = "stable"
        scope_merge_rates[scope] = {
            "current_rate": cur_rate,
            "prev_rate": prev_rate,
            "delta": delta,
            "trend": scope_trend,
            "created": cur_created,
            "merged": cur.get("merged", 0),
            "calibrated_in": cur.get("calibrated_in", 0),
            "calibrated_out": cur.get("calibrated_out", 0),
        }

    # Medium-scope review iterations: average iteration count for medium-scope terminal tasks
    medium_scope_tasks_with_loop = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("scope") == "medium"
        and (t.context or {}).get("review_loop") is not None
    ]
    medium_scope_review_iterations: float | None = None
    if medium_scope_tasks_with_loop:
        total_iters = sum(
            (t.context or {}).get("review_loop", {}).get("iteration", 0)
            for t in medium_scope_tasks_with_loop
        )
        medium_scope_review_iterations = round(total_iters / len(medium_scope_tasks_with_loop), 2)

    current_first_attempt_rate = _first_attempt_rate(terminal_tasks_24h)

    # Previous window for first_attempt trend comparison
    prev_terminal_tasks = prev_completed + prev_failed
    prev_first_attempt_rate = _first_attempt_rate(prev_terminal_tasks)

    first_attempt_trend: str | None = None
    if current_first_attempt_rate is not None and prev_first_attempt_rate is not None:
        fa_delta = current_first_attempt_rate - prev_first_attempt_rate
        if fa_delta > 0.05:
            first_attempt_trend = "improving"
        elif fa_delta < -0.05:
            first_attempt_trend = "declining"
        else:
            first_attempt_trend = "stable"

    # First-attempt failures by review category: which categories caused the most
    # first-attempt rejections, as a fraction of tasks that had their first review rejected.
    first_attempt_by_review_category: Dict[str, float] | None = None
    first_attempt_failed_with_cats = [
        t for t in terminal_tasks_24h
        if "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if first_attempt_failed_with_cats:
        total_with_cats = len(first_attempt_failed_with_cats)
        cat_counts: Dict[str, int] = {}
        for t in first_attempt_failed_with_cats:
            cats = (t.context or {}).get("_first_attempt_rejection_categories", {})
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        first_attempt_by_review_category = {
            cat: round(count / total_with_cats, 2)
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
        }

    def _arch_rejection_rate(tasks: list) -> float | None:
        failed = [t for t in tasks if "_first_attempt_rejection_categories" in (t.context or {})]
        if not failed:
            return None
        arch = sum(1 for t in failed if "architecture" in (t.context or {}).get("_first_attempt_rejection_categories", {}))
        return round(arch / len(failed), 2)

    first_attempt_success_rate = {
        "overall": current_first_attempt_rate,
        "trend": first_attempt_trend,
        "with_playbook": _first_attempt_rate(tasks_with_playbook) if tasks_with_playbook else None,
        "without_playbook": _first_attempt_rate(tasks_without_playbook) if tasks_without_playbook else None,
        "with_arch_context": _first_attempt_rate(tasks_with_arch_context) if tasks_with_arch_context else None,
        "without_arch_context": _first_attempt_rate(tasks_without_arch_context) if tasks_without_arch_context else None,
        "arch_rejection_rate_with_context": _arch_rejection_rate(tasks_with_arch_context),
        "arch_rejection_rate_without_context": _arch_rejection_rate(tasks_without_arch_context),
        "with_review_measurements": _first_attempt_rate(tasks_with_review_measurements) if tasks_with_review_measurements else None,
        "without_review_measurements": _first_attempt_rate(tasks_without_review_measurements) if tasks_without_review_measurements else None,
        "sample_size": len([t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]),
        "by_type": first_attempt_by_type,
        "by_scope": first_attempt_by_scope,
        "first_attempt_by_review_category": first_attempt_by_review_category,
        "with_measurements": _first_attempt_rate(tasks_with_measurements) if tasks_with_measurements else None,
        "without_measurements": _first_attempt_rate(tasks_without_measurements) if tasks_without_measurements else None,
    }

    # First-review approval rate: of create/refactor tasks that merged, what fraction
    # were approved on the first review (i.e., no _first_attempt_rejection_categories)?
    # This is the true measure of verification gate effectiveness.
    _fra_by_type: Dict[str, float] = {}
    for task_type, tasks in by_type.items():
        rate = _first_review_approval_rate(tasks)
        if rate is not None:
            _fra_by_type[task_type] = rate

    _fra_by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope.items():
        rate = _first_review_approval_rate(tasks)
        if rate is not None:
            _fra_by_scope[scope] = rate

    _review_eligible_merged = [
        t for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type", "create") in _review_eligible_types
        and (t.context or {}).get("pr_merged") is True
    ]
    first_review_approval_rate = {
        "overall": _first_review_approval_rate(terminal_tasks_24h),
        "sample_size": len(_review_eligible_merged),
        "by_type": _fra_by_type,
        "by_scope": _fra_by_scope,
        "with_playbook": _first_review_approval_rate(tasks_with_playbook),
        "without_playbook": _first_review_approval_rate(tasks_without_playbook),
        "with_measurements": _first_review_approval_rate(tasks_with_measurements),
        "without_measurements": _first_review_approval_rate(tasks_without_measurements),
    }

    # Self-review effectiveness: correlate self-review verdicts with first-attempt approval outcomes
    tasks_with_self_review = [
        t for t in terminal_tasks_24h
        if "_self_review_outcome" in (t.context or {})
    ]
    tasks_without_self_review = [
        t for t in terminal_tasks_24h
        if "_self_review_outcome" not in (t.context or {})
    ]
    tasks_self_review_clean = [
        t for t in tasks_with_self_review
        if not (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]
    tasks_self_review_fixed = [
        t for t in tasks_with_self_review
        if (t.context or {}).get("_self_review_outcome", {}).get("fixes_applied", False)
    ]

    self_review_effectiveness: Dict[str, Any] | None = None
    if len(tasks_with_self_review) >= 3:
        # Category-level correlation: for each check category, compare first-attempt rate
        # between tasks where that category flagged issues vs tasks where it passed clean.
        all_categories: set = set()
        for t in tasks_with_self_review:
            check_results = (t.context or {}).get("_self_review_outcome", {}).get("check_results") or {}
            all_categories.update(check_results.keys())

        category_correlation: Dict[str, Any] = {}
        for cat in all_categories:
            tasks_cat_findings = [
                t for t in tasks_with_self_review
                if not (t.context or {}).get("_self_review_outcome", {}).get("check_results", {}).get(cat, {}).get("passed", True)
            ]
            tasks_cat_clean = [
                t for t in tasks_with_self_review
                if cat in ((t.context or {}).get("_self_review_outcome", {}).get("check_results") or {})
                and (t.context or {}).get("_self_review_outcome", {}).get("check_results", {}).get(cat, {}).get("passed", True)
            ]
            cat_findings_rate = _first_attempt_rate(tasks_cat_findings) if tasks_cat_findings else None
            cat_clean_rate = _first_attempt_rate(tasks_cat_clean) if tasks_cat_clean else None
            if cat_findings_rate is not None or cat_clean_rate is not None:
                category_correlation[cat] = {
                    "with_findings_approval_rate": cat_findings_rate,
                    "clean_approval_rate": cat_clean_rate,
                }

        self_review_effectiveness = {
            "with_self_review_approval_rate": _first_attempt_rate(tasks_with_self_review),
            "without_self_review_approval_rate": _first_attempt_rate(tasks_without_self_review),
            "clean_verdict_approval_rate": _first_attempt_rate(tasks_self_review_clean),
            "fixed_verdict_approval_rate": _first_attempt_rate(tasks_self_review_fixed),
            "sample_size": len(tasks_with_self_review),
            "category_correlation": category_correlation,
        }

    # Review accuracy: precision and recall from user feedback signals on completed review tasks
    review_tasks = [
        t for t in terminal_tasks_24h
        if (t.first_payload or {}).get("type") == "review"
        and (t.context or {}).get("blueprint_review_verdict") is not None
    ]
    review_accuracy: Dict[str, Any] | None = None
    if len(review_tasks) >= 5:
        total_reviews = len(review_tasks)
        overrides = sum(1 for t in review_tasks if (t.context or {}).get("user_overrode_review"))
        dismissals = sum(1 for t in review_tasks if (t.context or {}).get("review_dismissed"))
        misses = sum(1 for t in review_tasks if (t.context or {}).get("user_caught_issues"))
        total_approvals = sum(
            1 for t in review_tasks
            if (t.context or {}).get("blueprint_review_verdict") == "APPROVE"
        )
        precision = round(1 - (overrides + dismissals) / total_reviews, 2)
        recall = round(1 - misses / total_approvals, 2) if total_approvals > 0 else None
        review_accuracy = {
            "total_reviews": total_reviews,
            "overrides": overrides,
            "dismissals": dismissals,
            "misses": misses,
            "total_approvals": total_approvals,
            "precision": precision,
            "recall": recall,
        }

    # Persistent review issues and full review effectiveness trends from
    # compute_review_effectiveness stored in project.design_doc.
    persistent_review_issues: Optional[List[str]] = None
    review_effectiveness_trends: Optional[Dict[str, Any]] = None
    review_recurrence_by_category: Optional[Dict[str, float]] = None
    project = await session.get(Project, project_id)
    if project and project.design_doc:
        eff = project.design_doc.get("review_effectiveness") or {}
        issues = eff.get("persistent_issues") or []
        if issues:
            persistent_review_issues = issues
        if eff:
            review_effectiveness_trends = {
                "computed_at": eff.get("computed_at"),
                "persistent_issues": eff.get("persistent_issues") or [],
                "categories": eff.get("categories") or {},
            }
        cats = eff.get("categories") or {}
        recurrence: Dict[str, float] = {}
        for cat, cat_data in cats.items():
            rate = cat_data.get("recurrence_rate")
            if rate is not None:
                recurrence[cat] = rate
        if recurrence:
            review_recurrence_by_category = recurrence

    direction_first_attempt_feedback: Optional[Dict[str, Any]] = None
    if project and project.design_doc:
        dfa = project.design_doc.get("direction_first_attempt_feedback")
        if dfa:
            direction_first_attempt_feedback = dfa

    # Self-review prediction accuracy: compare self-review check_results against human rejection categories
    self_review_prediction_accuracy: Dict[str, Any] | None = None
    tasks_with_both = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_self_review_outcome", {}).get("check_results")
        and "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if len(tasks_with_both) >= 5:
        total_human_cats = 0
        total_overlap = 0
        total_missed = 0
        total_false_alarms = 0
        total_sr_failed_cats = 0
        for t in tasks_with_both:
            ctx = t.context or {}
            check_results = ctx.get("_self_review_outcome", {}).get("check_results") or {}
            human_cats = set(ctx.get("_first_attempt_rejection_categories") or {})
            sr_failed = {cat for cat, res in check_results.items() if not res.get("passed", True)}
            overlap = human_cats & sr_failed
            missed = human_cats - sr_failed
            false_alarms = sr_failed - human_cats
            total_human_cats += len(human_cats)
            total_overlap += len(overlap)
            total_missed += len(missed)
            total_false_alarms += len(false_alarms)
            total_sr_failed_cats += len(sr_failed)
        self_review_prediction_accuracy = {
            "prediction_hit_rate": round(total_overlap / total_human_cats, 3) if total_human_cats > 0 else None,
            "miss_rate": round(total_missed / total_human_cats, 3) if total_human_cats > 0 else None,
            "false_alarm_rate": round(total_false_alarms / total_sr_failed_cats, 3) if total_sr_failed_cats > 0 else None,
            "sample_size": len(tasks_with_both),
        }

    # Medium-scope pre/post investment measurement — computed over a longer lookback
    # so research agents see the actual before/after merge rates straight from the prompt
    # instead of hitting /medium-scope-measurement (admin-auth only; agent tokens 401).
    medium_scope_measurement: Dict[str, Any] | None = None
    try:
        ms_cutoff = _now - timedelta(days=365)
        ms_result = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
                Task.created_at >= ms_cutoff,
            )
        )
        ms_all = ms_result.scalars().all()
        ms_medium = [t for t in ms_all if (t.context or {}).get("scope") == "medium"]
        if ms_medium:
            full = _compute_medium_scope_measurement(ms_medium, project, None, 365)
            medium_scope_measurement = {
                "medium_scope_merge_rate": full.get("medium_scope_merge_rate"),
                "total_created": full.get("total_created"),
                "total_merged": full.get("total_merged"),
                "pre_investment": full.get("pre_investment"),
                "post_investment": full.get("post_investment"),
                "recent_30d": full.get("recent_30d"),
                "recent_7d": full.get("recent_7d"),
                "investment_start": full.get("investment_start"),
            }
    except Exception:
        logger.exception("[ANALYTICS] medium_scope_measurement computation failed (non-critical)")

    from app.research_completion import SCOPE_FILE_BUDGETS  # noqa: PLC0415

    return {
        "success_rate": success_rate,
        "trend": trend,
        "failure_distribution": failure_distribution,
        "failure_taxonomy": failure_taxonomy,
        "direction_stats": direction_stats,
        "first_attempt_success_rate": first_attempt_success_rate,
        "first_review_approval_rate": first_review_approval_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        "self_review_effectiveness": self_review_effectiveness,
        "self_review_prediction_accuracy": self_review_prediction_accuracy,
        "review_accuracy": review_accuracy,
        "persistent_review_issues": persistent_review_issues,
        "review_effectiveness_trends": review_effectiveness_trends,
        "review_recurrence_by_category": review_recurrence_by_category,
        "scope_merge_rates": scope_merge_rates,
        "scope_file_budgets": SCOPE_FILE_BUDGETS,
        "medium_scope_review_iterations": medium_scope_review_iterations,
        "direction_first_attempt_feedback": direction_first_attempt_feedback,
        "medium_scope_measurement": medium_scope_measurement,
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Return a catch-up digest of activity within the requested time window."""
    window_start = _utcnow() - timedelta(hours=hours)
    prev_window_start = window_start - timedelta(hours=hours)
    # Extend lookback to capture long-running tasks and 7 periods for trend history
    raw_lookback = _utcnow() - timedelta(hours=min(hours * 7, 24 * 14))
    lookback_start = min(raw_lookback, prev_window_start)

    # --- Query tasks from extended lookback window ---
    # Load terminal tasks (COMPLETED/FAILED) for digest stats, plus any task
    # with requires_approval=True so we can surface pending-approval items in
    # needs_attention even while they are still IN_PROGRESS/QUEUED/etc.
    # QUEUED/PROVISIONING/IN_PROGRESS without approval flags are excluded here
    # to keep JSONB payload memory bounded; active counts come from a separate
    # lean query below.
    task_filters = [
        Task.created_at >= lookback_start,
        or_(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.requires_approval.is_(True),
        ),
    ]
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
        select(Task).where(*active_filters)
        .order_by(Task.started_at.asc().nullsfirst())
        .options(load_only(
            Task.id, Task.status, Task.title, Task.payloads,
            Task.started_at, Task.project_id,
        ))
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
    scope_merge_rates: Dict[str, Dict[str, int]] = {}
    prev_scope_merge_rates: Dict[str, Dict[str, int]] = {}

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

                # Single pass over research_history: aggregate PR/scope/product/type/taxonomy
                prs_merged = 0
                prs_rejected = 0
                findings_tasks_created = 0
                merge_time_values: List[float] = []
                dir_scope_agg: Dict[str, Dict[str, int]] = {}
                prev_dir_scope_agg: Dict[str, Dict[str, int]] = {}
                window_product_ratios: List[float] = []
                agg_type_distribution: Dict[str, int] = {}
                dir_taxonomy_summary: Dict[str, int] = {}
                recent_failed_titles: List[str] = []
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    except (ValueError, TypeError):
                        continue
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                        prs_rejected += entry.get("prs_rejected") or 0
                        findings_tasks_created += entry.get("tasks_created") or 0
                        atm = entry.get("avg_time_to_merge_hours")
                        if atm is not None:
                            merge_time_values.append(float(atm))
                        for scope, stats in (entry.get("scope_breakdown") or {}).items():
                            agg = dir_scope_agg.setdefault(scope, {"created": 0, "merged": 0, "failed": 0, "failed_infra": 0, "failed_code_error": 0, "failed_scope_overflow": 0, "calibrated_in": 0, "calibrated_out": 0})
                            for k in ("created", "merged", "failed", "failed_infra", "failed_code_error", "failed_scope_overflow", "calibrated_in", "calibrated_out"):
                                agg[k] += stats.get(k, 0)
                        pr = entry.get("product_ratio")
                        if pr is not None:
                            window_product_ratios.append(float(pr))
                        for ttype, cnt in (entry.get("type_distribution") or {}).items():
                            agg_type_distribution[ttype] = agg_type_distribution.get(ttype, 0) + cnt
                        for tax, cnt in (entry.get("taxonomy_summary") or {}).items():
                            dir_taxonomy_summary[tax] = dir_taxonomy_summary.get(tax, 0) + cnt
                        for fd in (entry.get("failed_task_details") or []):
                            title = (fd or {}).get("title")
                            if title:
                                recent_failed_titles.append(title)
                    elif entry_ts >= prev_window_start:
                        for scope, stats in (entry.get("scope_breakdown") or {}).items():
                            agg = prev_dir_scope_agg.setdefault(scope, {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0})
                            for k in ("created", "merged", "calibrated_in", "calibrated_out"):
                                agg[k] += stats.get(k, 0)

                if cycles_in_window == 0 and tasks_created_in_window == 0:
                    continue

                last_entry = (d.get("research_history") or [])[-1] if d.get("research_history") else None
                latest_insights = last_entry.get("cycle_insights", []) if last_entry else []
                recent_finding_titles = last_entry.get("finding_titles", []) if last_entry else []
                latest_research_at = last_entry.get("timestamp") if last_entry else None
                dir_product_ratio = round(sum(window_product_ratios) / len(window_product_ratios), 3) if window_product_ratios else None

                dir_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in tasks_for_dir if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start)

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                total_prs = prs_merged + prs_rejected
                acceptance_rate = round(prs_merged / total_prs, 3) if total_prs > 0 else None
                avg_time_to_merge_hours = round(sum(merge_time_values) / len(merge_time_values), 2) if merge_time_values else None
                conversion_rate = round(prs_merged / findings_tasks_created, 3) if findings_tasks_created > 0 else None
                # Accumulate into project-level scope rate aggregates
                for _s, _st in dir_scope_agg.items():
                    if _s not in scope_merge_rates:
                        scope_merge_rates[_s] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                    scope_merge_rates[_s]["created"] += _st["created"]
                    scope_merge_rates[_s]["merged"] += _st["merged"]
                    scope_merge_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    scope_merge_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)
                for _s, _st in prev_dir_scope_agg.items():
                    if _s not in prev_scope_merge_rates:
                        prev_scope_merge_rates[_s] = {"created": 0, "merged": 0, "calibrated_in": 0, "calibrated_out": 0}
                    prev_scope_merge_rates[_s]["created"] += _st["created"]
                    prev_scope_merge_rates[_s]["merged"] += _st["merged"]
                    prev_scope_merge_rates[_s]["calibrated_in"] += _st.get("calibrated_in", 0)
                    prev_scope_merge_rates[_s]["calibrated_out"] += _st.get("calibrated_out", 0)

                direction_progress_list.append({
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "prs_rejected": prs_rejected,
                    "acceptance_rate": acceptance_rate,
                    "conversion_rate": conversion_rate,
                    "avg_time_to_merge_hours": avg_time_to_merge_hours,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                    "product_ratio": dir_product_ratio,
                    "type_distribution": agg_type_distribution or None,
                    "estimated_cost_usd": round(dir_cost, 2),
                    "cost_per_merged_pr": round(dir_cost / prs_merged, 4) if prs_merged > 0 else None,
                    "scope_breakdown": dir_scope_agg or None,
                    "taxonomy_summary": dir_taxonomy_summary or None,
                    "recent_failure_titles": recent_failed_titles[-3:] or None,
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
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
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
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
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
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
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
        "SCOPE_OVERFLOW": "Scope Overflow",
    }
    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in failed_tasks:
        tid = str(t.id)
        cls = _classify_failure(t, error_log_map.get(tid))
        waiting_since = _derive_completed_at(t)
        waiting_hours = round((_now - waiting_since).total_seconds() / 3600) if waiting_since else None
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

    # Taxonomy distribution (4-category: scope/review/infra/code_defect)
    from app.failure_classification import (
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY, TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT, TAXONOMY_INFRA_RELATED,
    )
    _tax_keys = (
        TAXONOMY_SCOPE_RELATED, TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_INFRA_RELATED, TAXONOMY_CODE_DEFECT,
    )
    _tax_counts: Dict[str, int] = {k: 0 for k in _tax_keys}
    for t in failed_tasks:
        _tax_counts[_classify_taxonomy(t)] += 1
    _tax_total = sum(_tax_counts.values())
    failure_taxonomy_distribution = {
        "categories": {
            k: {
                "count": _tax_counts[k],
                "percentage": round(_tax_counts[k] / _tax_total * 100, 1) if _tax_total else 0.0,
            }
            for k in _tax_keys
        },
        "total_failures": _tax_total,
        "dominant_category": max(_tax_keys, key=lambda k: _tax_counts[k]) if _tax_total else None,
    }

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
    success_rate_delta: float | None = (
        round(success_rate - prev_success_rate, 2)
        if success_rate is not None and prev_success_rate is not None
        else None
    )
    if success_rate_delta is not None:
        if success_rate_delta > 0.05:
            trend: str | None = "improving"
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
        _p_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and _p_start <= t.vm_stopped_at < _p_end)
        _p_terminal = [
            t for t in all_tasks
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_measured = [t for t in _p_terminal if "_first_attempt_succeeded" in (t.context or {})]
        _p_fa_rate = round(sum(1 for t in _p_measured if (t.context or {}).get("_first_attempt_succeeded")) / len(_p_measured), 2) if _p_measured else None
        trend_history.append({
            "period_end": _p_end.isoformat(),
            "success_rate": round(_p_completed / _p_total, 2) if _p_total > 0 else None,
            "completed": _p_completed,
            "failed": _p_failed,
            "failed_by_class": _p_by_class,
            "period_cost_usd": round(_p_cost, 4),
            "first_attempt_success_rate": _p_fa_rate,
            "first_attempt_sample_size": len(_p_measured),
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
                    "SCOPE_OVERFLOW": f"Most failures ({cnt}/{failed_count}) exceeded scope budget (touched more files than scope allows) — consider task decomposition or scope escalation.",
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
    undirected_cost = sum(((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start and t.id not in directed_task_ids)
    total_estimated_cost_usd = round(sum(d["estimated_cost_usd"] for d in direction_progress_list) + undirected_cost, 2)

    # Compute today's cost for cost ceiling warning
    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_cost_usd = round(sum(
        ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE) * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        for t in all_tasks if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= today_start
    ), 2)
    daily_cost_ceiling_usd = float(_runtime_config.get("daily_cost_ceiling_usd", 0))

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
        "failure_taxonomy_distribution": failure_taxonomy_distribution,
        "recent_completions": recent_completions,
        "direction_progress": direction_progress_list,
        "overall_product_ratio": overall_product_ratio,
        "overall_type_distribution": overall_type_distribution or None,
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "daily_cost_usd": daily_cost_usd,
        "daily_cost_ceiling_usd": daily_cost_ceiling_usd,
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
        "scope_merge_rates": scope_merge_rates or None,
        "prev_scope_merge_rates": prev_scope_merge_rates or None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/health-overview
# ---------------------------------------------------------------------------

@router.get("/health-overview")
async def get_health_overview(
    project_id: uuid.UUID | None = Query(None),
    lookback_hours: int = Query(24, ge=1, le=8760),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return a unified health overview with top actionable issues."""
    from app.shared_state import external_health, circuit_breakers, get_circuit_breaker
    from app.compute import get_zone_health_summary
    from app.routers.admin_settings import _build_stuck_tasks

    now = _utcnow()
    actionable_issues: List[Dict[str, Any]] = []

    # --- 1. Success rate ---
    if project_id:
        health_summary = await compute_pipeline_health_summary(session, project_id, lookback_hours=lookback_hours)
        success_rate_value = health_summary.get("success_rate")
        sr_trend = health_summary.get("trend")
    else:
        window_start = now - timedelta(hours=lookback_hours)
        prev_window_start = window_start - timedelta(hours=lookback_hours)
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
        sr_trend: str | None = None
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
        "window_hours": lookback_hours,
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
    active_result = await session.execute(
        select(Task).where(*active_filters).options(
            load_only(Task.id, Task.status, Task.title, Task.project_id,
                      Task.created_at, Task.started_at, Task.last_heartbeat,
                      Task.vm_instance_id, Task.depends_on)
        )
    )
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


# ---------------------------------------------------------------------------
# GET /api/admin/direction-auto-pause-stats
# ---------------------------------------------------------------------------


def _compute_direction_auto_pause_stats(
    projects: List[Project],
    milestone_events: List[ProjectMessage],
    now: datetime,
    window_hours: int,
) -> Dict[str, Any]:
    """Aggregate direction auto-pause/recovery metrics over a window.

    Uses event timestamps from ProjectMessage(event_type=direction_milestone)
    for windowed counts and live Project.directions for currently-paused state.
    """
    window_start = now - timedelta(hours=window_hours)
    prev_window_start = window_start - timedelta(hours=window_hours)

    cur_paused = 0
    cur_retired = 0
    prev_paused = 0
    reason_counts: Dict[str, int] = defaultdict(int)

    for msg in milestone_events:
        ctx = msg.context or {}
        if ctx.get("event_type") != "direction_milestone":
            continue
        milestone = ctx.get("milestone")
        ts = msg.created_at
        if ts is None:
            continue
        if ts >= window_start:
            if milestone == "auto-paused":
                cur_paused += 1
                reason = (ctx.get("reason") or "").strip()
                if reason:
                    reason_counts[reason[:120]] += 1
            elif milestone == "auto-retired":
                cur_retired += 1
        elif ts >= prev_window_start:
            if milestone == "auto-paused":
                prev_paused += 1

    # Recovery proxy: paused directions in window that were not auto-retired in window.
    # Negative values are clamped — retirements can reference directions paused earlier.
    recovered = max(cur_paused - cur_retired, 0)

    # Currently auto-paused (live state — not window-bounded) so operators know active load.
    currently_auto_paused = 0
    for proj in projects:
        for d in (proj.directions or []):
            if d.get("status") == "paused" and d.get("auto_paused"):
                currently_auto_paused += 1

    top_reasons = [
        {"reason": r, "count": c}
        for r, c in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    ]

    if cur_paused > prev_paused + 1:
        trend = "up"
    elif cur_paused + 1 < prev_paused:
        trend = "down"
    else:
        trend = "flat"

    return {
        "window_hours": window_hours,
        "auto_paused_count": cur_paused,
        "auto_recovered_count": recovered,
        "auto_retired_count": cur_retired,
        "currently_auto_paused": currently_auto_paused,
        "top_reasons": top_reasons,
        "trend": trend,
        "prev_window_auto_paused_count": prev_paused,
    }


@router.get("/direction-auto-pause-stats")
async def get_direction_auto_pause_stats(
    project_id: uuid.UUID | None = Query(None),
    window_hours: int = Query(24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate direction auto-pause/recovery stats over a rolling window.

    Powers a dashboard widget that makes self-healing efficacy visible: how many
    directions were auto-paused, how many recovered vs. were auto-retired, the
    most common pause reasons, and a coarse trend vs. the previous window.
    """
    now = _utcnow()
    earliest = now - timedelta(hours=window_hours * 2)

    # Load live directions for the "currently paused" count
    if project_id:
        proj = await session.get(Project, project_id)
        projects = [proj] if proj else []
        msg_filters = [
            ProjectMessage.project_id == project_id,
            ProjectMessage.from_type == "system",
            ProjectMessage.created_at >= earliest,
        ]
    else:
        projects = (await session.execute(select(Project))).scalars().all()
        msg_filters = [
            ProjectMessage.from_type == "system",
            ProjectMessage.created_at >= earliest,
        ]

    events = (await session.execute(select(ProjectMessage).where(*msg_filters))).scalars().all()
    return _compute_direction_auto_pause_stats(projects, events, now, window_hours)


@router.get("/task-duration-stats")
async def get_task_duration_stats(
    project_id: uuid.UUID | None = Query(None),
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


@router.get("/failing-task-types")
async def get_failing_task_types(
    current_user: dict = Depends(require_admin),
):
    """Return task types currently gated by the orchestrator's per-type failure rate.

    Recomputes the same logic the dispatch loop uses (rate >= 0.7 with n >= 3
    in the trailing 15-minute window) from the shared task_results buffer, so
    operators can see which types are silently being skipped without reading
    logs.  Also reports the global failure_gate_threshold so callers can tell
    whether anomaly response has tightened it (0.6) vs the default (0.8).
    """
    from app import shared_state as _ss
    PER_TYPE_THRESHOLD = 0.7
    PER_TYPE_MIN_SAMPLES = 3
    WINDOW_MINUTES = 15

    cutoff = (_utcnow() - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    recent = [r for r in _ss.deploy_monitoring["task_results"] if r[0] >= cutoff]
    seen_types = {r[3] for r in recent if len(r) > 3 and r[3]}

    gated: List[Dict[str, Any]] = []
    for ttype in sorted(seen_types):
        rate, sample_size = _ss.get_task_type_failure_rate(ttype, window_minutes=WINDOW_MINUTES)
        if sample_size >= PER_TYPE_MIN_SAMPLES and rate >= PER_TYPE_THRESHOLD:
            gated.append({
                "task_type": ttype,
                "failure_rate": round(rate, 3),
                "sample_size": sample_size,
                "threshold": PER_TYPE_THRESHOLD,
            })

    global_threshold = _ss.anomaly_adjustments.get("failure_gate_threshold", 0.8)
    return {
        "gated_types": gated,
        "global_failure_threshold": global_threshold,
        "anomaly_tightened": bool(_ss.anomaly_adjustments.get("active")),
        "window_minutes": WINDOW_MINUTES,
        "generated_at": _utcnow().isoformat(),
    }


@router.get("/scope-baselines")
async def get_scope_baselines(
    project_id: uuid.UUID | None = Query(None),
    lookback_days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return per-scope merge rate baselines from historical terminal tasks.

    Queries COMPLETED/FAILED/CANCELLED tasks over the lookback window, groups
    them by ISO week and scope (context['scope']), and returns weekly time-series
    plus overall summary stats per scope with trend direction.
    """
    cutoff = _utcnow() - timedelta(days=lookback_days)
    filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
        Task.created_at >= cutoff,
    ]
    if project_id:
        filters.append(Task.project_id == project_id)

    result = await session.execute(
        select(Task).where(*filters).options(
            load_only(Task.id, Task.status, Task.context, Task.created_at, Task.project_id)
        )
    )
    tasks = result.scalars().all()

    return _compute_scope_baselines(list(tasks), lookback_days)


def _weekly_merge_rate_deltas(tasks: list) -> List[Dict[str, Any]]:
    """Bucket tasks by week and compute per-week merge rate + delta vs prior week.

    Shared between medium-scope measurement and strategic payload enrichment
    (backend/app/strategic_spawning.py::_build_recent_interventions).

    Returns a list of per-week dicts sorted chronologically:
        {week: ISO Monday date, created: int, merged: int,
         merge_rate: float|None, merge_rate_delta: float|None}
    """
    week_bucket: Dict[str, Dict[str, int]] = defaultdict(lambda: {"created": 0, "merged": 0})
    for t in tasks:
        if not t.created_at:
            continue
        task_date = t.created_at.date()
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        week_bucket[week_key]["created"] += 1
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
            week_bucket[week_key]["merged"] += 1

    sorted_weeks = sorted(week_bucket.keys())
    out: List[Dict[str, Any]] = []
    for i, week_key in enumerate(sorted_weeks):
        counts = week_bucket[week_key]
        created = counts["created"]
        merged = counts["merged"]
        merge_rate = round(merged / created, 3) if created > 0 else None
        if i == 0:
            delta = None
        else:
            prev_rate = out[i - 1]["merge_rate"]
            delta = round(merge_rate - prev_rate, 3) if (merge_rate is not None and prev_rate is not None) else None
        out.append({
            "week": week_key,
            "created": created,
            "merged": merged,
            "merge_rate": merge_rate,
            "merge_rate_delta": delta,
        })
    return out


def _compute_medium_scope_measurement(
    medium_tasks: list,
    project: Optional[Any],
    direction_id: Optional[str],
    lookback_days: int,
) -> Dict[str, Any]:
    """Compute medium-scope merge rate measurement with pre/post investment split.

    Returns headline metrics, investment split, weekly time-series with direction
    correlation, and a correlated_changes list showing direction-merged tasks
    alongside that week's merge-rate delta.
    """
    # --- Headline ---
    total_created = len(medium_tasks)
    total_merged = sum(
        1 for t in medium_tasks
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
    )
    medium_scope_merge_rate = round(total_merged / total_created, 3) if total_created > 0 else None

    # --- Find target direction ---
    resolved_direction_id: Optional[str] = direction_id
    target_direction = None
    if project and project.directions:
        directions = project.directions or []
        if resolved_direction_id:
            for d in directions:
                if d.get("id") == resolved_direction_id:
                    target_direction = d
                    break
        else:
            # Search for medium-scope direction by text
            for d in directions:
                text = (d.get("text") or "").lower()
                if "medium" in text and "scope" in text:
                    target_direction = d
                    resolved_direction_id = d.get("id")
                    break

    # --- Investment start from earliest research_history entry ---
    investment_start: Optional[datetime] = None
    if target_direction:
        research_history = target_direction.get("research_history") or []
        timestamps: list = []
        for entry in research_history:
            ts = entry.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                    timestamps.append(dt)
                except (ValueError, AttributeError):
                    pass
        if timestamps:
            investment_start = min(timestamps)

    _TERMINAL_STATUSES = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    def _window_stats(tasks_subset: list, window_start: Optional[datetime], window_end: Optional[datetime]) -> Dict[str, Any]:
        """Count ``tasks_subset`` once, using terminal-updated-in-window
        semantics. Callers that pre-filter by created_at produce in-flight
        bias on short windows — use ``_rolling_window_stats`` for those.
        Kept for pre/post-investment comparisons where the window spans
        months and everything is long-terminal."""
        finished = [t for t in tasks_subset if t.status in _TERMINAL_STATUSES]
        created = len(finished)
        merged = sum(
            1 for t in finished
            if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
        )
        return {
            "merge_rate": round(merged / created, 3) if created > 0 else None,
            "created": created,
            "merged": merged,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
        }

    def _rolling_window_stats(all_tasks: list, since: datetime) -> Dict[str, Any]:
        """Count tasks that reached a *terminal* state inside the window.

        Filtering by created_at for short windows produces severe in-flight
        bias: a medium-scope task takes ~60-90min end-to-end, so a 24h
        window of "tasks created in window" includes many still running,
        deflating the merge_rate (1/16 reads as 6% when the real rate is
        40%+). Bias is worst for slower scopes (medium, large), which
        would make the strategic agent panic on healthy data.

        This helper answers the operator question directly: "in this
        window, what fraction of the work that finished actually merged?"
        """
        now = _utcnow()
        finished = 0
        merged = 0
        spawned = 0
        for t in all_tasks:
            if t.created_at and t.created_at >= since:
                spawned += 1
            if t.status not in _TERMINAL_STATUSES:
                continue
            resolved_at = t.updated_at or t.created_at
            if not resolved_at or resolved_at < since:
                continue
            finished += 1
            if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
                merged += 1
        return {
            "merge_rate": round(merged / finished, 3) if finished > 0 else None,
            "created": finished,  # legacy alias — denominator of merge_rate
            "merged": merged,
            "finished": finished,
            "spawned": spawned,
            "window_start": since.isoformat(),
            "window_end": now.isoformat(),
        }

    # --- Pre/post split ---
    pre_investment: Optional[Dict[str, Any]] = None
    post_investment: Optional[Dict[str, Any]] = None
    if investment_start is not None:
        pre_tasks = [t for t in medium_tasks if t.created_at and t.created_at < investment_start]
        post_tasks = [t for t in medium_tasks if t.created_at and t.created_at >= investment_start]

        pre_start = min((t.created_at for t in pre_tasks if t.created_at), default=None)
        post_end = max((t.created_at for t in post_tasks if t.created_at), default=None)
        pre_investment = _window_stats(pre_tasks, pre_start, investment_start)
        post_investment = _window_stats(post_tasks, investment_start, post_end)

    # --- Recent rolling windows (independent of investment_start) ---
    # 24h and 3d added 2026-04-17: at 20-40 tasks/day, 7d+ windows drown recent
    # movement in old data. Overlapping windows let operators see "is this
    # a 24h blip or a 7d trend" at a glance, mirroring the strategic agent's
    # rolling_rates view. Using _rolling_window_stats (terminal-updated-in-window,
    # not created-in-window) avoids the in-flight bias that made a healthy
    # 40%+ medium merge rate read as 6% in a 24h window.
    _now = _utcnow()
    recent_24h_start = _now - timedelta(hours=24)
    recent_3d_start = _now - timedelta(days=3)
    recent_7d_start = _now - timedelta(days=7)
    recent_30d_start = _now - timedelta(days=30)
    recent_24h = _rolling_window_stats(medium_tasks, recent_24h_start)
    recent_3d = _rolling_window_stats(medium_tasks, recent_3d_start)
    recent_7d = _rolling_window_stats(medium_tasks, recent_7d_start)
    recent_30d = _rolling_window_stats(medium_tasks, recent_30d_start)
    # Suppress empty tiles (no finished tasks in window) to preserve the
    # existing "no data" rendering behaviour in the dashboard.
    if recent_24h["finished"] == 0:
        recent_24h = None
    if recent_3d["finished"] == 0:
        recent_3d = None
    if recent_7d["finished"] == 0:
        recent_7d = None
    if recent_30d["finished"] == 0:
        recent_30d = None

    # --- Weekly time series ---
    week_bucket: Dict[str, Dict[str, int]] = defaultdict(lambda: {"created": 0, "merged": 0, "direction_merges": 0})

    for t in medium_tasks:
        if not t.created_at:
            continue
        task_date = t.created_at.date()
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        week_bucket[week_key]["created"] += 1
        is_merged = t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
        if is_merged:
            week_bucket[week_key]["merged"] += 1
        if is_merged and resolved_direction_id and (t.context or {}).get("direction_id") == resolved_direction_id:
            week_bucket[week_key]["direction_merges"] += 1

    sorted_weeks = sorted(week_bucket.keys())
    weekly_series: List[Dict[str, Any]] = []
    for i, week_key in enumerate(sorted_weeks):
        counts = week_bucket[week_key]
        created = counts["created"]
        merged = counts["merged"]
        merge_rate = round(merged / created, 3) if created > 0 else None
        if i == 0:
            merge_rate_delta = None
        else:
            prev_rate = weekly_series[i - 1]["merge_rate"]
            merge_rate_delta = round(merge_rate - prev_rate, 3) if (merge_rate is not None and prev_rate is not None) else None
        weekly_series.append({
            "week": week_key,
            "created": created,
            "merged": merged,
            "merge_rate": merge_rate,
            "direction_merges": counts["direction_merges"],
            "merge_rate_delta": merge_rate_delta,
        })

    week_rate_delta: Dict[str, Optional[float]] = {e["week"]: e["merge_rate_delta"] for e in weekly_series}

    # --- Daily time series (for finer-grained trend chart) ---
    # Kept alongside weekly_series so existing UI / callers don't break.
    # Daily buckets are the right granularity at this pipeline's velocity
    # (20-40 medium tasks/day) — weekly reads as "one dot per week" and
    # hides within-week movement.
    # Bucket by shipped-on-day, not created-on-day: today's bucket should
    # reflect "what merged/failed today" rather than "of what started
    # today so far, how much merged?" (the latter is always low because
    # many tasks are still running).
    daily_cutoff = _now - timedelta(days=lookback_days)
    day_bucket: Dict[str, Dict[str, int]] = defaultdict(lambda: {"created": 0, "merged": 0, "direction_merges": 0})
    for t in medium_tasks:
        if t.status not in _TERMINAL_STATUSES:
            continue
        resolved_at = t.updated_at or t.created_at
        if not resolved_at or resolved_at < daily_cutoff:
            continue
        day_key = resolved_at.date().isoformat()
        day_bucket[day_key]["created"] += 1  # legacy key = denominator (finished-in-day)
        is_merged = t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
        if is_merged:
            day_bucket[day_key]["merged"] += 1
        if is_merged and resolved_direction_id and (t.context or {}).get("direction_id") == resolved_direction_id:
            day_bucket[day_key]["direction_merges"] += 1

    sorted_days = sorted(day_bucket.keys())
    daily_series: List[Dict[str, Any]] = []
    for i, day_key in enumerate(sorted_days):
        counts = day_bucket[day_key]
        created = counts["created"]
        merged = counts["merged"]
        merge_rate = round(merged / created, 3) if created > 0 else None
        if i == 0:
            merge_rate_delta = None
        else:
            prev_rate = daily_series[i - 1]["merge_rate"]
            merge_rate_delta = round(merge_rate - prev_rate, 3) if (merge_rate is not None and prev_rate is not None) else None
        daily_series.append({
            "day": day_key,
            "created": created,
            "merged": merged,
            "merge_rate": merge_rate,
            "direction_merges": counts["direction_merges"],
            "merge_rate_delta": merge_rate_delta,
        })

    # --- Correlated changes: direction-associated merged tasks with weekly delta ---
    correlated_changes: List[Dict[str, Any]] = []
    if resolved_direction_id:
        for t in medium_tasks:
            if (t.context or {}).get("direction_id") != resolved_direction_id:
                continue
            if not (t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")):
                continue
            if not t.created_at:
                continue
            task_date = t.created_at.date()
            week_monday = task_date - timedelta(days=task_date.weekday())
            week_key = week_monday.isoformat()
            correlated_changes.append({
                "title": t.title,
                "week": week_key,
                "merge_rate_delta": week_rate_delta.get(week_key),
            })

    return {
        "medium_scope_merge_rate": medium_scope_merge_rate,
        "total_created": total_created,
        "total_merged": total_merged,
        "investment_start": investment_start.isoformat() if investment_start else None,
        "pre_investment": pre_investment,
        "post_investment": post_investment,
        "recent_30d": recent_30d,
        "recent_7d": recent_7d,
        "recent_3d": recent_3d,
        "recent_24h": recent_24h,
        "weekly_series": weekly_series,
        "daily_series": daily_series,
        "correlated_changes": correlated_changes,
    }


def _compute_first_attempt_measurement(
    tasks: list,
    split_date: Optional[datetime],
    lookback_days: int,
) -> Dict[str, Any]:
    """Compute historical first-attempt approval rate with optional pre/post split.

    Returns headline rate, sample_size, optional pre/post split windows, weekly
    time-series (using _derive_completed_at for timestamps), and by_category
    breakdown of rejection reasons from _first_attempt_rejection_categories.
    """
    def _fa_rate(task_list: list) -> Optional[float]:
        measured = [t for t in task_list if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        successes = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(successes / len(measured), 2)

    measured_tasks = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
    headline_rate = _fa_rate(tasks)
    sample_size = len(measured_tasks)

    # --- Pre/post split using split_date and _derive_completed_at ---
    pre_split: Optional[Dict[str, Any]] = None
    post_split: Optional[Dict[str, Any]] = None
    if split_date is not None:
        split_naive = split_date.replace(tzinfo=None) if split_date.tzinfo else split_date
        pre_tasks = [t for t in measured_tasks if _derive_completed_at(t).replace(tzinfo=None) < split_naive]
        post_tasks = [t for t in measured_tasks if _derive_completed_at(t).replace(tzinfo=None) >= split_naive]

        def _window_stats(task_subset: list, window_start: Optional[datetime], window_end: Optional[datetime]) -> Dict[str, Any]:
            return {
                "rate": _fa_rate(task_subset),
                "sample_size": len(task_subset),
                "window_start": window_start.isoformat() if window_start else None,
                "window_end": window_end.isoformat() if window_end else None,
            }

        pre_start = min((_derive_completed_at(t) for t in pre_tasks), default=None)
        post_end = max((_derive_completed_at(t) for t in post_tasks), default=None)
        pre_split = _window_stats(pre_tasks, pre_start, split_naive)
        post_split = _window_stats(post_tasks, split_naive, post_end)

    # --- Weekly time series using _derive_completed_at for timestamp bucketing ---
    week_bucket: Dict[str, Dict[str, int]] = defaultdict(lambda: {"measured": 0, "succeeded": 0})
    for t in measured_tasks:
        completed = _derive_completed_at(t)
        task_date = completed.date()
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        week_bucket[week_key]["measured"] += 1
        if t.context["_first_attempt_succeeded"]:
            week_bucket[week_key]["succeeded"] += 1

    sorted_weeks = sorted(week_bucket.keys())
    weekly_series: List[Dict[str, Any]] = []
    for i, week_key in enumerate(sorted_weeks):
        counts = week_bucket[week_key]
        measured = counts["measured"]
        succeeded = counts["succeeded"]
        rate = round(succeeded / measured, 2) if measured > 0 else None
        if i == 0:
            delta = None
        else:
            prev_rate = weekly_series[i - 1]["rate"]
            delta = round(rate - prev_rate, 2) if (rate is not None and prev_rate is not None) else None
        weekly_series.append({
            "week": week_key,
            "rate": rate,
            "sample_size": measured,
            "delta_from_previous": delta,
        })

    # --- By-category breakdown from _first_attempt_rejection_categories ---
    tasks_with_cats = [t for t in tasks if "_first_attempt_rejection_categories" in (t.context or {})]
    by_category: Optional[Dict[str, float]] = None
    if tasks_with_cats:
        total_with_cats = len(tasks_with_cats)
        cat_counts: Dict[str, int] = {}
        for t in tasks_with_cats:
            cats = (t.context or {}).get("_first_attempt_rejection_categories", {})
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        by_category = {
            cat: round(count / total_with_cats, 2)
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
        }

    return {
        "first_attempt_approval_rate": headline_rate,
        "sample_size": sample_size,
        "pre_split": pre_split,
        "post_split": post_split,
        "weekly_series": weekly_series,
        "by_category": by_category,
        "lookback_days": lookback_days,
    }


@router.get("/medium-scope-measurement")
async def get_medium_scope_measurement(
    project_id: uuid.UUID = Query(...),
    direction_id: Optional[str] = Query(None),
    lookback_days: int = Query(365, ge=1, le=730),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return medium-scope merge rate measurement with pre/post investment split.

    Queries terminal tasks for the project, filters to medium scope, and computes
    headline metrics, pre/post investment windows, weekly time-series with
    direction correlation, and correlated_changes for shipped direction tasks.
    """
    cutoff = _utcnow() - timedelta(days=lookback_days)
    result = await session.execute(
        select(Task).where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            Task.created_at >= cutoff,
            Task.project_id == project_id,
        )
    )
    all_tasks = result.scalars().all()
    medium_tasks = [t for t in all_tasks if (t.context or {}).get("scope") == "medium"]

    project = await session.get(Project, project_id)

    return _compute_medium_scope_measurement(medium_tasks, project, direction_id, lookback_days)


@router.get("/first-attempt-measurement")
async def get_first_attempt_measurement(
    project_id: uuid.UUID = Query(...),
    lookback_days: int = Query(90, ge=1, le=730),
    split_date: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return historical first-attempt approval rate with optional pre/post split.

    Queries all terminal tasks for the project within lookback_days and computes
    headline first-attempt rate, optional pre/post split windows, weekly time-series,
    and by_category breakdown of rejection reasons.
    """
    cutoff = _utcnow() - timedelta(days=lookback_days)
    result = await session.execute(
        select(Task).where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            Task.created_at >= cutoff,
            Task.project_id == project_id,
        )
    )
    all_tasks = result.scalars().all()

    parsed_split_date: Optional[datetime] = None
    if split_date:
        try:
            parsed_split_date = datetime.fromisoformat(split_date)
        except (ValueError, TypeError):
            pass


    return _compute_first_attempt_measurement(list(all_tasks), parsed_split_date, lookback_days)


ORCHESTRATOR_ERRORS_CACHE_TTL_SECONDS = 60
ORCHESTRATOR_SERVICE_NAME = os.getenv("ORCHESTRATOR_SERVICE_NAME", "blueprint-orchestrator")
_orchestrator_errors_cache: Dict[Tuple[int, int], Tuple[float, Dict[str, Any]]] = {}

# Acknowledged error buckets are persisted in the OrchestratorErrorAck table.
# Key is "exception_class|source" for a specific bucket, or "*" for a global ack.
# Buckets whose last_seen <= acknowledged_at are filtered from responses.

_EXCEPTION_LINE_RE = re.compile(
    r"^(?P<cls>(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning|Timeout|NotFound|Denied|Conflict))(?::|\b)",
    re.MULTILINE,
)
_TRACEBACK_FRAME_RE = re.compile(
    r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[^\n]+)',
)


def _extract_error_message(entry: Any) -> str:
    """Return the textual payload from a Cloud Logging entry, regardless of shape."""
    payload = getattr(entry, "payload", None)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("message", "msg", "exception", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(payload, default=str)[:4000]
    if payload is None:
        # No text payload — extract container-level metadata from the entry
        parts: list[str] = []
        severity = getattr(entry, "severity", None)
        if severity:
            parts.append(f"severity={severity}")
        resource = getattr(entry, "resource", None)
        if resource:
            labels = getattr(resource, "labels", None) or {}
            if isinstance(labels, dict):
                for k in ("revision_name", "configuration_name"):
                    if labels.get(k):
                        parts.append(f"{k}={labels[k]}")
        http_req = getattr(entry, "http_request", None)
        if http_req and isinstance(http_req, dict):
            method = http_req.get("requestMethod", "")
            url = http_req.get("requestUrl", "")
            status = http_req.get("status", "")
            if method or url:
                parts.append(f"{method} {url} → {status}")
        labels = getattr(entry, "labels", None)
        if labels and isinstance(labels, dict):
            for k in ("instanceId", "run.googleapis.com/execution_environment"):
                if labels.get(k):
                    parts.append(f"{k}={labels[k]}")
        if parts:
            return "[Cloud Run system] " + " | ".join(parts)
        return ""
    return str(payload)[:4000]


def _parse_exception_class(message: str) -> str:
    """Scan a log payload for the last exception class name in a traceback."""
    if not message:
        return "UnknownError"
    if message.startswith("[Cloud Run system]"):
        return "ContainerError"
    matches = _EXCEPTION_LINE_RE.findall(message)
    if matches:
        return matches[-1].rstrip(":")
    for line in reversed(message.strip().splitlines()):
        stripped = line.strip()
        if stripped and stripped[0].isupper() and ("Error" in stripped or "Exception" in stripped):
            head = stripped.split(":", 1)[0].strip()
            if head:
                return head
    return "UnknownError"


def _parse_source_location(message: str) -> Tuple[str, int | None]:
    """Return the first traceback frame inside the app source tree."""
    app_frame: Tuple[str, int] | None = None
    last_frame: Tuple[str, int] | None = None
    for match in _TRACEBACK_FRAME_RE.finditer(message):
        path = match.group("path")
        try:
            line_no = int(match.group("line"))
        except ValueError:
            continue
        last_frame = (path, line_no)
        if "/app/app/" in path or path.startswith("app/") or "\\app\\app\\" in path:
            app_frame = (path, line_no)
    chosen = app_frame or last_frame
    if chosen is None:
        return ("unknown", None)
    path, line_no = chosen
    marker = "/app/app/"
    idx = path.find(marker)
    if idx != -1:
        return (path[idx + len("/app/"):], line_no)
    return (path, line_no)


def _parse_entry_timestamp(entry: Any) -> datetime:
    ts = getattr(entry, "timestamp", None)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _bucket_log_entries(entries: List[Any], limit: int) -> List[Dict[str, Any]]:
    """Group log entries by (exception_class, source_file, source_line)."""
    buckets: Dict[Tuple[str, str, int | None], Dict[str, Any]] = {}
    for entry in entries:
        message = _extract_error_message(entry)
        exception_class = _parse_exception_class(message)
        source_file, source_line = _parse_source_location(message)
        key = (exception_class, source_file, source_line)
        ts = _parse_entry_timestamp(entry)
        bucket = buckets.get(key)
        if bucket is None:
            sample = message.strip()
            if len(sample) > 3000:
                sample = sample[:3000] + "..."
            buckets[key] = {
                "exception_class": exception_class,
                "source": f"{source_file}:{source_line}" if source_line else source_file,
                "count": 1,
                "first_seen_iso": ts.isoformat(),
                "last_seen_iso": ts.isoformat(),
                "sample_message": sample,
            }
            continue
        bucket["count"] += 1
        if ts.isoformat() < bucket["first_seen_iso"]:
            bucket["first_seen_iso"] = ts.isoformat()
        if ts.isoformat() > bucket["last_seen_iso"]:
            bucket["last_seen_iso"] = ts.isoformat()
    ranked = sorted(
        buckets.values(),
        key=lambda b: (b["count"], b["last_seen_iso"]),
        reverse=True,
    )
    return ranked[:limit]


def _fetch_orchestrator_error_entries(hours: int) -> List[Any]:
    """Query Cloud Logging for ERROR-severity entries from the orchestrator service."""
    from google.cloud import logging_v2

    gcp_project_id = os.getenv("GCP_PROJECT_ID", "")
    if not gcp_project_id or gcp_project_id == "your-gcp-project-id":
        raise RuntimeError("GCP_PROJECT_ID not configured")

    client = logging_v2.Client(project=gcp_project_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filter_str = (
        'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{ORCHESTRATOR_SERVICE_NAME}" '
        'severity>=ERROR '
        f'timestamp>="{cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    entries_iter = client.list_entries(
        filter_=filter_str,
        order_by=logging_v2.DESCENDING,
        page_size=500,
    )
    collected: List[Any] = []
    for entry in entries_iter:
        collected.append(entry)
        if len(collected) >= 500:
            break
    return collected


def _parse_bucket_last_seen(last_seen_iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(last_seen_iso)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _filter_acknowledged_buckets(
    buckets: List[Dict[str, Any]],
    acks: Dict[str, datetime],
) -> List[Dict[str, Any]]:
    """Remove buckets whose last_seen is at or before the acknowledged timestamp."""
    if not acks:
        return buckets
    global_ack = acks.get("*")
    filtered = []
    for b in buckets:
        last_seen = _parse_bucket_last_seen(b["last_seen_iso"])
        if last_seen is None:
            filtered.append(b)
            continue
        key = f"{b['exception_class']}|{b['source']}"
        ack_ts = acks.get(key)
        if ack_ts and last_seen <= ack_ts:
            continue
        if global_ack and last_seen <= global_ack:
            continue
        filtered.append(b)
    return filtered


async def _load_acknowledgements(session: AsyncSession) -> Dict[str, datetime]:
    """Return all persisted error-bucket acknowledgements as a key→datetime map."""
    result = await session.execute(select(OrchestratorErrorAck))
    return {row.key: row.acknowledged_at for row in result.scalars().all()}


async def _upsert_ack(session: AsyncSession, key: str, when: datetime, by: str) -> None:
    existing = await session.get(OrchestratorErrorAck, key)
    if existing is None:
        session.add(OrchestratorErrorAck(key=key, acknowledged_at=when, acknowledged_by=by))
    else:
        existing.acknowledged_at = when
        existing.acknowledged_by = by


@router.get("/recent-orchestrator-errors")
async def get_recent_orchestrator_errors(
    hours: int = Query(default=1, ge=1, le=24),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate ERROR-severity logs from Cloud Logging for the orchestrator service."""
    acks = await _load_acknowledgements(session)
    cache_key = (hours, limit)
    now = time.time()
    cached = _orchestrator_errors_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < ORCHESTRATOR_ERRORS_CACHE_TTL_SECONDS:
        cached_payload = cached[1]
        cached_buckets = cached_payload.get("buckets", [])
        visible = _filter_acknowledged_buckets(cached_buckets, acks)
        return {**cached_payload, "buckets": visible, "acknowledged_count": len(cached_buckets) - len(visible)}

    try:
        entries = _fetch_orchestrator_error_entries(hours)
    except Exception as exc:
        logger.exception("Failed to fetch orchestrator error logs from Cloud Logging")
        payload: Dict[str, Any] = {
            "buckets": [],
            "hours": hours,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _orchestrator_errors_cache[cache_key] = (now, payload)
        return payload

    all_buckets = _bucket_log_entries(entries, limit)
    _orchestrator_errors_cache[cache_key] = (now, {
        "buckets": all_buckets,
        "hours": hours,
        "total_entries_scanned": len(entries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    visible = _filter_acknowledged_buckets(all_buckets, acks)
    return {
        "buckets": visible,
        "hours": hours,
        "total_entries_scanned": len(entries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "acknowledged_count": len(all_buckets) - len(visible),
    }


@router.post("/orchestrator-errors/acknowledge")
async def acknowledge_orchestrator_error(
    exception_class: str = Query(...),
    source: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Acknowledge a specific orchestrator error bucket so it's hidden until new occurrences arrive."""
    key = f"{exception_class}|{source}"
    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    await _upsert_ack(session, key, _utcnow(), user_email)
    await session.commit()
    logger.info(f"[ADMIN] Orchestrator error acknowledged: {key} by {user_email}")
    return {"status": "acknowledged", "key": key}


@router.post("/orchestrator-errors/acknowledge-all")
async def acknowledge_all_orchestrator_errors(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Acknowledge all current orchestrator error buckets."""
    now_dt = _utcnow()
    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    acknowledged_keys: List[str] = []
    seen: set[str] = set()
    for _cached_ts, cached_payload in _orchestrator_errors_cache.values():
        for b in cached_payload.get("buckets", []):
            key = f"{b['exception_class']}|{b['source']}"
            if key in seen:
                continue
            seen.add(key)
            await _upsert_ack(session, key, now_dt, user_email)
            acknowledged_keys.append(key)
    # If cache is empty but caller wants to ack everything, set a global marker
    if not acknowledged_keys:
        await _upsert_ack(session, "*", now_dt, user_email)
    await session.commit()
    logger.info(f"[ADMIN] All orchestrator errors acknowledged ({len(acknowledged_keys)} buckets) by {user_email}")
    return {"status": "acknowledged", "count": len(acknowledged_keys)}


@router.delete("/orchestrator-errors/acknowledged")
async def clear_acknowledged_orchestrator_errors(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Clear all acknowledgments so previously hidden errors reappear."""
    from sqlalchemy import delete as sa_delete
    result = await session.execute(sa_delete(OrchestratorErrorAck))
    await session.commit()
    count = result.rowcount or 0
    return {"status": "cleared", "count": count}


# ---------------------------------------------------------------------------
# Open PR monitor
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = {
    TaskStatus.QUEUED, TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING,
}


# Server-side cache for the open-PRs dashboard widget.  Dashboard polls at
# 30s; without caching, every open tab = 120 GitHub calls/hr to /pulls.
# With a 60s cache, total is capped at 60/hr regardless of how many
# browser tabs are open.
_OPEN_PRS_CACHE_TTL_SECONDS = 60
_open_prs_cache: Dict[uuid.UUID, Tuple[float, dict]] = {}


@router.get("/open-prs")
async def get_open_prs(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return all open PRs from GitHub cross-referenced with linked tasks."""
    from app.services import _get_project_github_token
    from app.github import get_open_pull_requests

    project = (await session.execute(
        select(Project).where(Project.id == project_id)
    )).scalars().first()
    if not project or not project.github_owner or not project.github_repo:
        return {"open_prs": []}

    # Cache check (skipping early paths above because github_owner/repo can
    # change and we want to honor that immediately).
    cached = _open_prs_cache.get(project_id)
    if cached and time.time() - cached[0] < _OPEN_PRS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        token = await _get_project_github_token(project, session)
    except Exception:
        logger.warning("Failed to get GitHub token for project %s", project_id)
        return {"open_prs": []}
    if not token:
        return {"open_prs": []}

    github_prs = await get_open_pull_requests(
        token, project.github_owner, project.github_repo
    )

    # Build task lookup: pr_number -> list of linked active tasks.
    # Terminal statuses (COMPLETED/FAILED/CANCELLED) are excluded — the widget
    # is for in-flight work. Dedupe by task.id since a task can match via
    # both a PR artifact and a context pr_number.
    pr_task_map: Dict[int, Dict[uuid.UUID, Task]] = {}

    # Source 1: PR artifacts (agent-created PRs — extract number from URL)
    artifact_rows = (await session.execute(
        select(Artifact, Task)
        .join(Task, Artifact.task_id == Task.id)
        .where(
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
            Task.project_id == project_id,
            Task.status.in_(list(_ACTIVE_STATUSES)),
        )
    )).all()
    for art, task in artifact_rows:
        try:
            pr_num = int(art.pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            continue
        pr_task_map.setdefault(pr_num, {})[task.id] = task

    # Source 2: Tasks with pr_number in context (review/fix pipeline + rollups)
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(list(_ACTIVE_STATUSES)),
        )
    )).scalars().all()
    for t in active_tasks:
        ctx = t.context or {}
        pr_num = (
            (ctx.get("review_loop") or {}).get("pr_number")
            or ctx.get("rollup_pr_number")
        )
        if pr_num and isinstance(pr_num, int):
            pr_task_map.setdefault(pr_num, {})[t.id] = t

    result = []
    for gh_pr in github_prs:
        pr_number = gh_pr.get("number")
        pr_url = gh_pr.get("html_url", "")
        linked = list(pr_task_map.get(pr_number, {}).values())
        linked.sort(key=lambda t: t.created_at or datetime.min, reverse=True)
        tasks_payload = [
            {
                "task_id": str(t.id),
                "task_status": t.status,
                "task_type": t.first_payload.get("type", "create"),
                "task_title": t.title,
            }
            for t in linked
        ]
        result.append({
            "pr_number": gh_pr.get("number"),
            "pr_url": pr_url,
            "pr_title": gh_pr.get("title", ""),
            "pr_branch": (gh_pr.get("head") or {}).get("ref", ""),
            "pr_created_at": gh_pr.get("created_at"),
            "tasks": tasks_payload,
            "is_orphaned": len(tasks_payload) == 0,
        })

    payload = {"open_prs": result}
    _open_prs_cache[project_id] = (time.time(), payload)
    return payload
