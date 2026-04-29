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
import app.shared_state as _shared_state
from app.compute import get_zone_health_summary
from app.routers.admin_digest import compute_pipeline_health_summary
from app.routers.admin_debug import _build_stuck_tasks

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-health"],
    dependencies=[Depends(get_current_user)]
)


@router.get("/health-overview")
async def get_health_overview(
    project_id: uuid.UUID | None = Query(None),
    lookback_hours: int = Query(24, ge=1, le=720),
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
        # Phase B4 (H-P4): cap unbounded scan at 5000 rows (was 20000) and
        # warn if truncated. The status_log JSONB column is required because
        # _derive_at() reads its last entry. Other heavy JSONBs (context,
        # payloads, plan_output) stay deferred via load_only.
        _XP_LIMIT = 5000
        xp_result = await session.execute(
            select(Task).where(
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.created_at >= prev_window_start,
                Task.archived.is_(False),
            ).options(
                load_only(Task.id, Task.status, Task.created_at, Task.status_log)
            ).limit(_XP_LIMIT)
        )
        xp_tasks = xp_result.scalars().all()
        if len(xp_tasks) >= _XP_LIMIT:
            logger.warning(
                "[HEALTH-OVERVIEW] success-rate scan hit cap=%d rows over %dh — "
                "result truncated; consider narrowing the lookback window or "
                "adding a project_id filter",
                _XP_LIMIT, lookback_hours * 2,
            )

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
        "possibly_complete_count": 0,
        "missing_criteria_count": 0,
        "held_count": 0,
    }
    if project_id:
        project = await session.get(Project, project_id)
        projects_for_dirs = [project] if project else []
    else:
        all_projects_result = await session.execute(select(Project).limit(500))
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
            if d.get("possibly_complete"):
                direction_portfolio["possibly_complete_count"] += 1
            if not d.get("completion_criteria"):
                direction_portfolio["missing_criteria_count"] += 1
            if d.get("needs_manual_intervention") or d.get("auto_paused"):
                direction_portfolio["held_count"] += 1
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


# --- Warm-VM continuity observability ----------------------------------------
# Surfaces the VM⇄task assignments behind the warm-VM continuity feature so
# operators can see at a glance: which VMs are alive, what task each is running,
# how many warm-pinned tasks are queued, and how often the orchestrator is
# clearing warm pins (a leading indicator of warm-VM instability).

@router.get("/warm-vm-state")
async def get_warm_vm_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Snapshot of active VMs and warm-pin chain state for the admin UI."""
    now = _utcnow()

    # Build the "set of VMs we care about": union of (a) IDs heartbeating, (b)
    # IDs running an IN_PROGRESS task, (c) IDs targeted by a QUEUED warm-pin.
    heartbeat_vms = set(_shared_state.vm_last_heartbeat.keys())

    in_progress_stmt = (
        select(Task)
        .where(Task.status == TaskStatus.IN_PROGRESS, Task.vm_instance_id.is_not(None))
        .options(load_only(
            Task.id, Task.vm_instance_id, Task.title, Task.payloads,
            Task.status, Task.context, Task.started_at, Task.project_id,
        ))
    )
    in_progress_tasks = (await session.execute(in_progress_stmt)).scalars().all()
    inprogress_by_vm: Dict[str, Task] = {}
    for t in in_progress_tasks:
        if t.vm_instance_id and t.vm_instance_id not in inprogress_by_vm:
            inprogress_by_vm[t.vm_instance_id] = t

    pinned_stmt = (
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .options(load_only(Task.id, Task.context, Task.payloads, Task.project_id))
    )
    pinned_tasks = (await session.execute(pinned_stmt)).scalars().all()
    pinned_by_vm: Dict[str, List[Task]] = {}
    for t in pinned_tasks:
        ctx = t.context or {}
        target = ctx.get("warm_target_vm_id")
        if target:
            pinned_by_vm.setdefault(target, []).append(t)

    completed_stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.vm_instance_id.is_not(None),
            Task.last_heartbeat >= now - timedelta(hours=1),
        )
        .options(load_only(
            Task.id, Task.vm_instance_id, Task.payloads, Task.last_heartbeat,
            Task.status, Task.title,
        ))
        .order_by(Task.last_heartbeat.desc())
        .limit(200)
    )
    completed_tasks = (await session.execute(completed_stmt)).scalars().all()
    completed_by_vm: Dict[str, List[Task]] = {}
    for t in completed_tasks:
        if t.vm_instance_id:
            bucket = completed_by_vm.setdefault(t.vm_instance_id, [])
            if len(bucket) < 5:
                bucket.append(t)

    all_vms = heartbeat_vms | inprogress_by_vm.keys() | pinned_by_vm.keys() | completed_by_vm.keys()

    def _payload_type(t: Task) -> str:
        try:
            payloads = t.payloads or []
            if payloads and isinstance(payloads[0], dict):
                return str(payloads[0].get("type") or "")
        except Exception:  # noqa: BLE001 — best-effort metadata
            pass
        return ""

    def _pr_number(t: Task) -> int | None:
        try:
            payloads = t.payloads or []
            if payloads and isinstance(payloads[0], dict):
                pr = payloads[0].get("pr_number")
                if isinstance(pr, int):
                    return pr
        except Exception:  # noqa: BLE001 — best-effort metadata
            pass
        ctx_pr = (t.context or {}).get("warm_target_pr")
        if isinstance(ctx_pr, int):
            return ctx_pr
        return None

    active_vms_out: List[Dict[str, Any]] = []
    for vm_id in sorted(all_vms):
        last_hb = _shared_state.vm_last_heartbeat.get(vm_id)
        is_alive = _shared_state.is_vm_alive(vm_id)

        cur = inprogress_by_vm.get(vm_id)
        cur_out: Dict[str, Any] | None = None
        if cur is not None:
            timing = (cur.context or {}).get("_timing") or {}
            cur_out = {
                "id": str(cur.id),
                "title": cur.title or "",
                "task_type": _payload_type(cur),
                "status": cur.status.value if hasattr(cur.status, "value") else str(cur.status),
                "warm_start": bool(timing.get("warm_start", False)),
                "prior_role_vm_id": timing.get("prior_role_vm_id"),
                "setup_secs": timing.get("setup_secs"),
                "started_at": cur.started_at.isoformat() if cur.started_at else None,
                "pr_number": _pr_number(cur),
                "project_id": str(cur.project_id) if cur.project_id else None,
            }

        pinned_out = []
        for t in pinned_by_vm.get(vm_id, []):
            ctx = t.context or {}
            pinned_out.append({
                "id": str(t.id),
                "task_type": _payload_type(t),
                "warm_target_pr": ctx.get("warm_target_pr") if isinstance(ctx.get("warm_target_pr"), int) else None,
                "warm_handoff_deadline": ctx.get("warm_handoff_deadline"),
            })

        recent_out = []
        for t in completed_by_vm.get(vm_id, []):
            recent_out.append({
                "id": str(t.id),
                "task_type": _payload_type(t),
                "completed_at": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            })

        active_vms_out.append({
            "vm_instance_id": vm_id,
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
            "is_alive": bool(is_alive),
            "current_task": cur_out,
            "warm_pinned_queued": pinned_out,
            "recent_completions": recent_out,
        })

    # Distinct VMs that currently host an IN_PROGRESS warm-start task.
    active_warm_chains = sum(
        1
        for t in in_progress_tasks
        if (t.context or {}).get("_timing", {}).get("warm_start") is True
    )

    return {
        "active_vms": active_vms_out,
        "warm_pin_metrics": {
            "clears_last_hour": _shared_state.count_recent_warm_pin_clears(3600),
            "clears_last_24h": _shared_state.count_recent_warm_pin_clears(86400),
            "active_pinned_queued_total": sum(len(v) for v in pinned_by_vm.values()),
            "active_warm_chains": active_warm_chains,
        },
        "warm_continuity_enabled": bool(_shared_state.runtime_config.get("warm_continuity_enabled", False)),
        "next_role_poll_timeout_secs": int(_shared_state.runtime_config.get("next_role_poll_timeout_secs", 180)),
    }
