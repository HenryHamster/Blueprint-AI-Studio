"""Unified health overview endpoint."""

import uuid
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session, resolve_dialect_name
from app.digest_cache import DigestCache, get_digest_cache
from app.models import (
    Task,
    TaskStatus,
    Project,
    DeploymentState,
    DeploymentStatus,
    _utcnow,
)
from app.auth import get_current_user, require_admin
from app.shared_state import (
    external_health,
    circuit_breakers,
    get_circuit_breaker,
    snapshot_pr_is_not_behind_counts,
)
import app.shared_state as _shared_state
from app.compute import get_zone_health_summary
from app.pipeline_health import compute_pipeline_health_summary
from app.routers.admin_debug import _build_stuck_tasks

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["admin-health"], dependencies=[Depends(get_current_user)]
)


@router.get("/health-overview")
async def get_health_overview(
    project_id: uuid.UUID | None = Query(None),
    lookback_hours: int = Query(24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Return a unified health overview with top actionable issues.

    Wraps the compute path in a TTL cache with in-flight dedup so that a
    stampede of concurrent admin UI polls (DashboardHome refreshes every
    30s) doesn't re-execute the multi-thousand-row JSONB scan for each
    request. Cache key is (project_id, lookback_hours) — the response does
    not vary per user. Default TTL 15s; tune in digest_cache.
    """
    cache_key = (
        "health-overview",
        str(project_id) if project_id else None,
        lookback_hours,
    )
    return await cache.get_or_compute(
        cache_key,
        lambda: _compute_health_overview(
            project_id=project_id,
            lookback_hours=lookback_hours,
            session=session,
        ),
    )


@router.get("/diagnostics/null-branch-config")
async def get_null_branch_config_diagnostics(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(Project)
                .where(
                    (Project.integration_branch.is_(None))
                    | (Project.promotion_target.is_(None))
                )
                .order_by(Project.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    projects = []
    self_repo_skipped = []
    for project in rows:
        item = {
            "id": str(project.id),
            "name": project.name,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "is_self_repo": project.is_self_repo,
            "created_at": project.created_at.isoformat()
            if project.created_at
            else None,
        }
        if project.is_self_repo and project.integration_branch is None:
            self_repo_skipped.append(item)
        else:
            projects.append(item)
    return {
        "projects": projects,
        "self_repo_skipped": self_repo_skipped,
        "total": len(projects) + len(self_repo_skipped),
    }


async def _compute_health_overview(
    *,
    project_id: uuid.UUID | None,
    lookback_hours: int,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Compute the unified health overview payload (cached by caller)."""
    # Fast-fail guard (see admin_digest._compute_digest): cap this scan under
    # the 120s global statement_timeout so a slow query releases its pooled
    # connection quickly rather than starving the shared pool; the cache layer
    # serves the last good result on timeout. Postgres-only.
    if resolve_dialect_name(session) == "postgresql":
        await session.execute(text("SET LOCAL statement_timeout = '30s'"))
    now = _utcnow()
    actionable_issues: List[Dict[str, Any]] = []

    # --- 1. Success rate ---
    if project_id:
        health_summary = await compute_pipeline_health_summary(
            session, project_id, lookback_hours=lookback_hours
        )
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
            select(Task)
            .where(
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.created_at >= prev_window_start,
                Task.archived.is_(False),
            )
            .options(load_only(Task.id, Task.status, Task.created_at, Task.status_log))
            .limit(_XP_LIMIT)
        )
        xp_tasks = xp_result.scalars().all()
        if len(xp_tasks) >= _XP_LIMIT:
            logger.warning(
                "[HEALTH-OVERVIEW] success-rate scan hit cap=%d rows over %dh — "
                "result truncated; consider narrowing the lookback window or "
                "adding a project_id filter",
                _XP_LIMIT,
                lookback_hours * 2,
            )

        def _derive_at(t: Task):
            if t.status_log:
                try:
                    return datetime.fromisoformat(t.status_log[-1]["t"])
                except (KeyError, ValueError):
                    pass
            return t.created_at

        xp_completed = [
            t
            for t in xp_tasks
            if t.status == TaskStatus.COMPLETED and _derive_at(t) >= window_start
        ]
        xp_failed = [
            t
            for t in xp_tasks
            if t.status == TaskStatus.FAILED and _derive_at(t) >= window_start
        ]
        xp_total = len(xp_completed) + len(xp_failed)
        success_rate_value = (
            round(len(xp_completed) / xp_total, 2) if xp_total > 0 else None
        )

        prev_completed = [
            t
            for t in xp_tasks
            if t.status == TaskStatus.COMPLETED
            and prev_window_start <= _derive_at(t) < window_start
        ]
        prev_failed = [
            t
            for t in xp_tasks
            if t.status == TaskStatus.FAILED
            and prev_window_start <= _derive_at(t) < window_start
        ]
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
    _ACTIVE_TASKS_LIMIT = 5000
    active_result = await session.execute(
        select(Task)
        .where(*active_filters)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.title,
                Task.project_id,
                Task.created_at,
                Task.started_at,
                Task.last_heartbeat,
                Task.vm_instance_id,
                Task.depends_on,
            )
        )
        .limit(_ACTIVE_TASKS_LIMIT)
    )
    active_tasks = active_result.scalars().all()
    if len(active_tasks) >= _ACTIVE_TASKS_LIMIT:
        logger.warning(
            "[HEALTH-OVERVIEW] active-tasks scan hit cap=%d rows — "
            "stuck-task diagnostics and queue-depth counts are truncated; "
            "consider filtering by project_id to reduce result size",
            _ACTIVE_TASKS_LIMIT,
        )

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
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"{count} task(s) stuck on VM provisioning",
                    "hint": hint,
                    "task_ids": task_ids,
                    "category": "stuck_tasks",
                }
            )
        elif reason == "no_heartbeat":
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"{count} task(s) lost heartbeat",
                    "hint": "The reaper will reclaim and retry these tasks automatically.",
                    "task_ids": task_ids,
                    "category": "stuck_tasks",
                }
            )
        elif reason == "long_queued":
            if available == 0:
                hint = "All zones exhausted — no capacity available. Check zone health."
            else:
                hint = "Tasks queued >30 min. Check whether max_concurrent_vms limit is reached."
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"{count} task(s) long-queued (>30 min)",
                    "hint": hint,
                    "task_ids": task_ids,
                    "category": "stuck_tasks",
                }
            )
        elif reason == "stuck_deps":
            all_dep_ids: List[str] = []
            for t in s_tasks:
                all_dep_ids.extend(t.get("unresolvable_deps", []))
            unique_deps = list(dict.fromkeys(all_dep_ids))
            dep_str = ", ".join(unique_deps[:5]) + (
                "..." if len(unique_deps) > 5 else ""
            )
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"{count} task(s) blocked by failed dependencies",
                    "hint": f"Blocking dep(s): {dep_str}. Cancel or requeue the blocking tasks.",
                    "task_ids": task_ids,
                    "category": "stuck_tasks",
                }
            )
        elif reason == "evaluation_timeout":
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"{count} task(s) stuck in evaluation (>2 h)",
                    "hint": "Evaluation exceeded the 2-hour threshold. Consider manual review or cancellation.",
                    "task_ids": task_ids,
                    "category": "stuck_tasks",
                }
            )

    # --- 3. Circuit breaker ---
    if project_id:
        cb = get_circuit_breaker(str(project_id))
        if cb["state"] == "OPEN":
            actionable_issues.append(
                {
                    "severity": "critical",
                    "title": "Circuit breaker OPEN — autonomous dispatch halted",
                    "hint": f"Too many consecutive failures tripped the breaker. Auto-recovers after {cb.get('cooldown_seconds', 1800) // 60} min cooldown.",
                    "task_ids": [],
                    "category": "circuit_breaker",
                }
            )
        elif cb["state"] == "HALF_OPEN":
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": "Circuit breaker HALF_OPEN — testing recovery",
                    "hint": "One test task is running to verify recovery. Monitor for success.",
                    "task_ids": [],
                    "category": "circuit_breaker",
                }
            )
    else:
        for proj_id, cb in circuit_breakers.items():
            if cb["state"] == "OPEN":
                actionable_issues.append(
                    {
                        "severity": "critical",
                        "title": f"Circuit breaker OPEN for project {proj_id[:8]}",
                        "hint": f"Auto-recovers after {cb.get('cooldown_seconds', 1800) // 60} min cooldown.",
                        "task_ids": [],
                        "category": "circuit_breaker",
                    }
                )
            elif cb["state"] == "HALF_OPEN":
                actionable_issues.append(
                    {
                        "severity": "warning",
                        "title": f"Circuit breaker HALF_OPEN for project {proj_id[:8]}",
                        "hint": "One test task running to verify recovery.",
                        "task_ids": [],
                        "category": "circuit_breaker",
                    }
                )

    # --- 4. Zone health ---
    exhausted_zones = zone_summary.get("exhausted", [])
    available_zones = zone_summary.get("available", 0)
    total_zones = available_zones + len(exhausted_zones)
    if total_zones > 0 and len(exhausted_zones) > total_zones / 2:
        actionable_issues.append(
            {
                "severity": "warning",
                "title": f"{len(exhausted_zones)}/{total_zones} zones exhausted",
                "hint": f"Exhausted: {', '.join(exhausted_zones)}. New task dispatch may be delayed.",
                "task_ids": [],
                "category": "zone_exhaustion",
            }
        )

    # --- 4b. Warm-VM continuity circuit breaker ---
    if _shared_state.runtime_config.get("warm_continuity_enabled", False):
        if _shared_state.is_warm_continuity_suppressed():
            sup_until = _shared_state.warm_continuity_suppressed_until
            clears = _shared_state.count_recent_warm_pin_clears(3600)
            threshold = _shared_state.runtime_config.get(
                "warm_pin_clear_rate_threshold", 10
            )
            cooldown_min = (
                _shared_state.runtime_config.get(
                    "warm_continuity_cooldown_seconds", 1800
                )
                // 60
            )
            sup_str = sup_until.strftime("%H:%M UTC") if sup_until else "unknown"
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": "Warm-VM continuity circuit breaker open — falling back to cold provisioning",
                    "hint": (
                        f"{clears} pin-clear events in last hour exceeded threshold ({threshold}). "
                        f"Auto-resumes at {sup_str} (~{cooldown_min} min cooldown)."
                    ),
                    "task_ids": [],
                    "category": "warm_vm_circuit_breaker",
                }
            )

    # --- 4c. Warm-backlog reuse circuit breaker ---
    if _shared_state.runtime_config.get("warm_backlog_reuse_enabled", False):
        if _shared_state.is_warm_backlog_reuse_suppressed():
            sup_until = _shared_state.warm_backlog_reuse_suppressed_until
            failures = _shared_state.count_recent_backlog_reuse_failures(3600)
            threshold = _shared_state.runtime_config.get(
                "warm_backlog_reuse_failure_rate_threshold", 5
            )
            cooldown_min = (
                _shared_state.runtime_config.get(
                    "warm_backlog_reuse_cooldown_seconds", 1800
                )
                // 60
            )
            sup_str = sup_until.strftime("%H:%M UTC") if sup_until else "unknown"
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": "Warm-backlog reuse circuit breaker open — falling back to cold provisioning",
                    "hint": (
                        f"{failures} backlog-reuse failure events in last hour exceeded threshold ({threshold}). "
                        f"Auto-resumes at {sup_str} (~{cooldown_min} min cooldown)."
                    ),
                    "task_ids": [],
                    "category": "warm_backlog_reuse_circuit_breaker",
                }
            )

    # --- 4d. Warm-pool claim circuit breaker ---
    if _shared_state.runtime_config.get("warm_pool_enabled", False):
        if _shared_state.is_warm_pool_suppressed():
            sup_until = _shared_state.warm_pool_suppressed_until
            failures = _shared_state.count_recent_pool_claim_failures(3600)
            threshold = _shared_state.runtime_config.get(
                "warm_pool_failure_rate_threshold", 5
            )
            cooldown_min = (
                _shared_state.runtime_config.get("warm_pool_cooldown_seconds", 1800)
                // 60
            )
            sup_str = sup_until.strftime("%H:%M UTC") if sup_until else "unknown"
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": "Warm-pool claim circuit breaker open — maintainer paused, dispatch falling back to cold provisioning",
                    "hint": (
                        f"{failures} pool-claim failure events in last hour exceeded threshold ({threshold}). "
                        f"Auto-resumes at {sup_str} (~{cooldown_min} min cooldown)."
                    ),
                    "task_ids": [],
                    "category": "warm_pool_circuit_breaker",
                }
            )

    # --- 5. External dependency health ---
    failed_deps = external_health.get("failed_deps", [])
    if failed_deps:
        actionable_issues.append(
            {
                "severity": "critical",
                "title": f"External dependency failure: {', '.join(failed_deps)}",
                "hint": "One or more external services unreachable. Check service status dashboards.",
                "task_ids": [],
                "category": "external_dep",
            }
        )

    # --- 6. Deploy state ---
    deploy_state = await session.get(DeploymentState, 1)
    if deploy_state:
        if deploy_state.status == DeploymentStatus.MONITORING:
            sha_short = (
                deploy_state.current_sha[:7] if deploy_state.current_sha else "unknown"
            )
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"Deploy {sha_short} in monitoring window",
                    "hint": "Current deploy is being monitored for stability. Avoid config changes.",
                    "task_ids": [],
                    "category": "deploy_state",
                }
            )
        elif deploy_state.status in (
            DeploymentStatus.PAUSED,
            DeploymentStatus.ROLLED_BACK,
            DeploymentStatus.HALTED,
        ):
            reason_msg = (
                deploy_state.pause_reason
                or deploy_state.rollback_reason
                or "Manual intervention may be required."
            )
            actionable_issues.append(
                {
                    "severity": "warning",
                    "title": f"Deploy status: {deploy_state.status}",
                    "hint": reason_msg,
                    "task_ids": [],
                    "category": "deploy_state",
                }
            )

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
            d
            for d in (proj.directions or [])
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
        "active": sum(
            1
            for t in active_tasks
            if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING)
        ),
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
    cache: DigestCache = Depends(get_digest_cache),
) -> Dict[str, Any]:
    """Snapshot of active VMs and warm-pin chain state for the admin UI.

    Wraps the compute path in a TTL cache with in-flight dedup so that a
    stampede of concurrent admin UI polls (the warm-VM panel refreshes every
    5-30s) doesn't re-execute the IN_PROGRESS+QUEUED+terminal scans for each
    request. Cache key is system-wide (no project filter); response is the
    same for every admin caller. Default TTL 15s; tune in digest_cache.
    """
    cache_key = ("warm-vm-state",)
    return await cache.get_or_compute(
        cache_key,
        lambda: _compute_warm_vm_state(session=session),
    )


# Phase F3 (H2): hard cap on each warm-vm-state scan. The endpoint is admin-
# polled every 5-30s and previously had no LIMIT — at scale the IN_PROGRESS
# scan alone could fan out to thousands of rows under burst load. Truncating
# to 1000 keeps the response bounded; we log a warning if the cap is hit so
# operators can extend the cap or narrow the query rather than silently
# missing VMs.
_WARM_VM_SCAN_LIMIT = 1000


async def _compute_warm_vm_state(*, session: AsyncSession) -> Dict[str, Any]:
    """Compute the warm-VM state payload (cached by caller)."""
    now = _utcnow()

    # Build the "set of VMs we care about": union of (a) IDs heartbeating, (b)
    # IDs running an IN_PROGRESS task, (c) IDs targeted by a QUEUED warm-pin.
    heartbeat_vms = set(_shared_state.vm_last_heartbeat.keys())

    # Phase F3 (H2): include status_log in load_only — even for read-only sites,
    # the Task event listeners (models.py ~360) read it on every status mutation
    # under async sessions, and an unloaded status_log triggers MissingGreenlet
    # if any downstream code touches Task.status. Safer to always include it.
    in_progress_stmt = (
        select(Task)
        .where(Task.status == TaskStatus.IN_PROGRESS, Task.vm_instance_id.is_not(None))
        .options(
            load_only(
                Task.id,
                Task.vm_instance_id,
                Task.title,
                Task.payloads,
                Task.status,
                Task.context,
                Task.started_at,
                Task.project_id,
                Task.status_log,
            )
        )
        .limit(_WARM_VM_SCAN_LIMIT)
    )
    in_progress_tasks = (await session.execute(in_progress_stmt)).scalars().all()
    if len(in_progress_tasks) >= _WARM_VM_SCAN_LIMIT:
        logger.warning(
            "[WARM-VM-STATE] in_progress scan hit cap=%d rows — result truncated; "
            "warm-VM panel may miss VMs",
            _WARM_VM_SCAN_LIMIT,
        )
    inprogress_by_vm: Dict[str, Task] = {}
    for t in in_progress_tasks:
        if t.vm_instance_id and t.vm_instance_id not in inprogress_by_vm:
            inprogress_by_vm[t.vm_instance_id] = t

    # Audit R3-W2 (M8): push the warm_target_vm_id existence filter into SQL
    # so we don't pull 1000-row pages of QUEUED tasks just to Python-filter
    # to a handful of warm-pinned ones. ``context['warm_target_vm_id']``
    # compiles to ``context -> 'warm_target_vm_id'`` (Postgres JSONB) /
    # ``json_extract(context, '$.warm_target_vm_id')`` (SQLite); both
    # return NULL when the key is absent, so the IS NOT NULL predicate is
    # dialect-agnostic. SQLite has no GIN — the index_safe variant matters
    # only on Postgres but the predicate evaluates correctly either way.
    # R4-W2 (M3): use ``has_key`` (compiles to JSONB ``?`` operator) so the
    # ``ix_task_context_gin`` GIN index can satisfy the predicate. The prior
    # ``as_string().is_not(None)`` form casts to text and never matches the
    # GIN index, forcing a SeqScan.
    pinned_where = [
        Task.status == TaskStatus.QUEUED,
        Task.context.has_key("warm_target_vm_id"),
    ]
    pinned_stmt = (
        select(Task)
        .where(*pinned_where)
        .options(
            load_only(
                Task.id,
                Task.context,
                Task.payloads,
                Task.project_id,
                Task.status_log,
            )
        )
        .limit(_WARM_VM_SCAN_LIMIT)
    )
    pinned_tasks = (await session.execute(pinned_stmt)).scalars().all()
    if len(pinned_tasks) >= _WARM_VM_SCAN_LIMIT:
        logger.warning(
            "[WARM-VM-STATE] pinned QUEUED scan hit cap=%d rows — result truncated",
            _WARM_VM_SCAN_LIMIT,
        )
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
        .options(
            load_only(
                Task.id,
                Task.vm_instance_id,
                Task.payloads,
                Task.last_heartbeat,
                Task.status,
                Task.title,
                Task.status_log,
            )
        )
        .order_by(Task.last_heartbeat.desc())
        .limit(_WARM_VM_SCAN_LIMIT)
    )
    completed_tasks = (await session.execute(completed_stmt)).scalars().all()
    if len(completed_tasks) >= _WARM_VM_SCAN_LIMIT:
        logger.warning(
            "[WARM-VM-STATE] recent completion scan hit cap=%d rows — result truncated",
            _WARM_VM_SCAN_LIMIT,
        )
    completed_by_vm: Dict[str, List[Task]] = {}
    for t in completed_tasks:
        if t.vm_instance_id:
            bucket = completed_by_vm.setdefault(t.vm_instance_id, [])
            if len(bucket) < 5:
                bucket.append(t)

    all_vms = (
        heartbeat_vms
        | inprogress_by_vm.keys()
        | pinned_by_vm.keys()
        | completed_by_vm.keys()
    )

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
                "status": cur.status.value
                if hasattr(cur.status, "value")
                else str(cur.status),
                "warm_start": bool(timing.get("warm_start", False)),
                "warm_claim_origin": timing.get("warm_claim_origin"),
                "prior_role_vm_id": timing.get("prior_role_vm_id"),
                "setup_secs": timing.get("setup_secs"),
                "started_at": cur.started_at.isoformat() if cur.started_at else None,
                "pr_number": _pr_number(cur),
                "project_id": str(cur.project_id) if cur.project_id else None,
            }

        pinned_out = []
        for t in pinned_by_vm.get(vm_id, []):
            ctx = t.context or {}
            pinned_out.append(
                {
                    "id": str(t.id),
                    "task_type": _payload_type(t),
                    "warm_target_pr": ctx.get("warm_target_pr")
                    if isinstance(ctx.get("warm_target_pr"), int)
                    else None,
                    "warm_handoff_deadline": ctx.get("warm_handoff_deadline"),
                }
            )

        recent_out = []
        for t in completed_by_vm.get(vm_id, []):
            recent_out.append(
                {
                    "id": str(t.id),
                    "task_type": _payload_type(t),
                    "completed_at": t.last_heartbeat.isoformat()
                    if t.last_heartbeat
                    else None,
                    "status": t.status.value
                    if hasattr(t.status, "value")
                    else str(t.status),
                }
            )

        active_vms_out.append(
            {
                "vm_instance_id": vm_id,
                "last_heartbeat": last_hb.isoformat() if last_hb else None,
                "is_alive": bool(is_alive),
                "current_task": cur_out,
                "warm_pinned_queued": pinned_out,
                "recent_completions": recent_out,
            }
        )

    # Distinct VMs that currently host an IN_PROGRESS warm-start task.
    active_warm_chains = sum(
        1
        for t in in_progress_tasks
        if (t.context or {}).get("_timing", {}).get("warm_start") is True
    )
    # Origin attribution: split the warm-start population into three buckets —
    # pool-origin claims (a pre-provisioned warm-pool member served the
    # dispatch), backlog-reuse claims (a VM picked up an unrelated QUEUED task
    # from the same project's backlog), and chain-continuity claims (the
    # existing PR-chain follow-on path, origin "next_role_endpoint" or legacy
    # unset). The three sum to active_warm_chains so operators can attribute the
    # reuse lift — and the pool bucket is isolated rather than folded into
    # chain-continuity (its entire reason to exist is measurable pool lift).
    from app.warm_pool import WARM_POOL_ORIGIN

    active_backlog_reuse_claims = sum(
        1
        for t in in_progress_tasks
        if (t.context or {}).get("_timing", {}).get("warm_start") is True
        and (t.context or {}).get("_timing", {}).get("warm_claim_origin")
        == "backlog_reuse"
    )
    active_pool_origin_claims = sum(
        1
        for t in in_progress_tasks
        if (t.context or {}).get("_timing", {}).get("warm_start") is True
        and (t.context or {}).get("_timing", {}).get("warm_claim_origin")
        == WARM_POOL_ORIGIN
    )
    active_chain_continuity_claims = (
        active_warm_chains - active_backlog_reuse_claims - active_pool_origin_claims
    )
    pool_origin_share = (
        round(active_pool_origin_claims / active_warm_chains, 4)
        if active_warm_chains
        else None
    )

    cb_suppressed = _shared_state.is_warm_continuity_suppressed()
    suppressed_until = _shared_state.warm_continuity_suppressed_until
    backlog_cb_suppressed = _shared_state.is_warm_backlog_reuse_suppressed()
    backlog_suppressed_until = _shared_state.warm_backlog_reuse_suppressed_until
    # Warm-pool claim breaker telemetry. is_warm_pool_suppressed() is called
    # first so the deadline is re-evaluated against the live failure rate (the
    # maintainer normally drives this each tick, but the health view should
    # reflect a breaker that is tripping right now), then pool_breaker_state()
    # snapshots the computed state for the diagnostics surface.
    from app.warm_pool_maintainer import pool_breaker_state

    _shared_state.is_warm_pool_suppressed()
    pool_breaker = pool_breaker_state(_shared_state.runtime_config)
    return {
        "active_vms": active_vms_out,
        "warm_pin_metrics": {
            "clears_last_hour": _shared_state.count_recent_warm_pin_clears(3600),
            "clears_last_24h": _shared_state.count_recent_warm_pin_clears(86400),
            "clears_by_reason_last_hour": _shared_state.count_recent_warm_pin_clears_by_reason(
                3600
            ),
            "clears_by_reason_last_24h": _shared_state.count_recent_warm_pin_clears_by_reason(
                86400
            ),
            "active_pinned_queued_total": sum(len(v) for v in pinned_by_vm.values()),
            "active_warm_chains": active_warm_chains,
            "active_backlog_reuse_claims": active_backlog_reuse_claims,
            "active_pool_origin_claims": active_pool_origin_claims,
            "active_chain_continuity_claims": active_chain_continuity_claims,
            "pool_origin_share": pool_origin_share,
            "circuit_breaker_suppressed": cb_suppressed,
            "suppressed_until": suppressed_until.isoformat()
            if suppressed_until
            else None,
            "clear_rate_threshold": int(
                _shared_state.runtime_config.get("warm_pin_clear_rate_threshold", 10)
            ),
            "backlog_reuse_circuit_breaker_suppressed": backlog_cb_suppressed,
            "backlog_reuse_suppressed_until": backlog_suppressed_until.isoformat()
            if backlog_suppressed_until
            else None,
            "backlog_reuse_failures_last_hour": _shared_state.count_recent_backlog_reuse_failures(
                3600
            ),
            "backlog_reuse_failure_rate_threshold": int(
                _shared_state.runtime_config.get(
                    "warm_backlog_reuse_failure_rate_threshold", 5
                )
            ),
            "pool_circuit_breaker_suppressed": bool(pool_breaker["suppressed"]),
            "pool_suppressed_until": pool_breaker["suppressed_until"],
            "pool_claim_failures_last_hour": pool_breaker["recent_failures_1h"],
            "pool_failure_rate_threshold": pool_breaker["failure_rate_threshold"],
        },
        "warm_continuity_enabled": bool(
            _shared_state.runtime_config.get("warm_continuity_enabled", False)
        ),
        "warm_backlog_reuse_enabled": bool(
            _shared_state.runtime_config.get("warm_backlog_reuse_enabled", False)
        ),
        "next_role_poll_timeout_secs": int(
            _shared_state.runtime_config.get("next_role_poll_timeout_secs", 180)
        ),
        "chain_continuity_funnel": _get_chain_continuity_funnel(),
        "spawn_pin_funnel": _get_spawn_pin_funnel(),
        "backlog_reuse_funnel": await _get_backlog_reuse_funnel(session),
        "pool_metrics": await _get_pool_metrics(session),
    }


async def _get_pool_metrics(session: AsyncSession) -> dict:
    """Warm-pool sizing + idle-cost + dispatch-funnel snapshot for the operator panel.

    Combines the runtime config (enabled flag, target size, idle TTL), the live
    PoolVm registry aggregate (current size, idle count, projected idle cost —
    banked + not-yet-settled accrual), and the in-memory pool-hit-vs-cold-dispatch
    funnel. Best-effort: returns {} on any failure so a partial backend never
    blanks the whole warm-VM panel.

    ``idle_cost_by_account`` and ``cost_amortization_by_account`` carry the
    per-account breakdown a controlled enable's 'retire on success or pivot'
    decision depends on: which account's idle members drive cost, the per-account
    warm-claim/lease-hit counts and hit rate, and the idle dollars spent per warm
    start. The funnel already embeds its own ``by_account`` block; the
    amortization view joins that with the per-account idle cost so an operator can
    read both sides per account from one map.
    """
    try:
        from app.efficiency_snapshot import (
            compute_pool_cost_amortization_by_account,
            compute_pool_idle_cost,
        )
        from app.warm_backlog_reuse import get_pool_funnel_snapshot
        from app.warm_pool import warm_pool_project_scope

        cost = await compute_pool_idle_cost(session)
        funnel = get_pool_funnel_snapshot()
        idle_by_account = cost.get("by_account") or {}
        return {
            "enabled": bool(
                _shared_state.runtime_config.get("warm_pool_enabled", False)
            ),
            # Current per-project enablement scope (sorted ids). Empty list means
            # the pool is fleet-wide (legacy all-or-nothing). The operator panel
            # renders this as the set of currently-scoped projects, each with a
            # disable control.
            "project_allowlist": sorted(warm_pool_project_scope()),
            "target_size": int(
                _shared_state.runtime_config.get("warm_pool_target_size", 2)
            ),
            "idle_ttl_secs": int(
                _shared_state.runtime_config.get("warm_pool_idle_ttl_secs", 1800)
            ),
            "current_size": cost["pool_size"],
            "idle_count": cost["idle_count"],
            "projected_idle_cost_usd": cost["total_idle_cost_usd"],
            "settled_idle_cost_usd": cost["settled_idle_cost_usd"],
            "live_idle_cost_usd": cost["live_idle_cost_usd"],
            "funnel": funnel,
            "idle_cost_by_account": idle_by_account,
            "cost_amortization_by_account": compute_pool_cost_amortization_by_account(
                idle_by_account, funnel
            ),
        }
    except Exception:  # noqa: BLE001 — best-effort metric, never blank the panel
        return {}


def _get_chain_continuity_funnel() -> dict:
    try:
        from app.warm_backlog_reuse import get_chain_continuity_funnel_snapshot

        return get_chain_continuity_funnel_snapshot()
    except Exception:
        return {}


def _get_spawn_pin_funnel() -> dict:
    try:
        from app.warm_backlog_reuse import get_spawn_pin_funnel_snapshot

        return get_spawn_pin_funnel_snapshot()
    except Exception:
        return {}


async def _get_backlog_reuse_funnel(session: AsyncSession) -> dict:
    try:
        from app.efficiency_snapshot import DESIGN_DOC_KEY
        from app.warm_backlog_reuse import (
            compute_dominant_reuse_dropout,
            get_reuse_funnel_snapshot,
        )

        snap = get_reuse_funnel_snapshot()
        snap.update(compute_dominant_reuse_dropout())
        rows = (await session.execute(
            select(Project).options(load_only(Project.id, Project.design_doc)).limit(500)
        )).scalars().all()
        buckets = []
        for project in rows:
            for entry in (
                (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
            ).get("snapshots") or []:
                block = entry.get("reuse_dropout")
                if not isinstance(block, dict):
                    continue
                dominant = block.get("dominant_dropout") or {}
                buckets.append(
                    {
                        "captured_at": entry.get("captured_at"),
                        "dropout_total": block.get("dropout_total"),
                        "dominant_reason": dominant.get("reason"),
                        "dominant_share": dominant.get("share"),
                    }
                )
        snap["trend"] = {"buckets": buckets[-90:]}
        return snap
    except Exception:
        return {}


@router.get("/pr-behind-detector-stats")
async def get_pr_behind_detector_stats(
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """In-process counts of pr_is_not_behind outcomes, keyed by call site.

    Each call-site entry has integer buckets ``true`` / ``false`` / ``none``
    plus ``total``. Counters are non-persistent (reset on process restart) and
    track the four canonical call sites listed in
    ``shared_state.PR_IS_NOT_BEHIND_CALL_SITES`` even when no hits have been
    recorded yet, so the response shape is stable from process start.
    """
    return {"pr_is_not_behind": snapshot_pr_is_not_behind_counts()}
