"""Unified health overview endpoint."""
import uuid
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.models import (
    Task, TaskStatus, Project, DeploymentState, DeploymentStatus, _utcnow,
)
from app.auth import get_current_user, require_admin
from app.shared_state import external_health, circuit_breakers, get_circuit_breaker
from app.compute import get_zone_health_summary
from app.routers.admin_digest import compute_pipeline_health_summary
from app.routers.admin_settings import _build_stuck_tasks

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-health"],
    dependencies=[Depends(get_current_user)]
)


@router.get("/health-overview")
async def get_health_overview(
    project_id: uuid.UUID | None = Query(None),
    lookback_hours: int = Query(24, ge=1, le=8760),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return a unified health overview with top actionable issues."""
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
