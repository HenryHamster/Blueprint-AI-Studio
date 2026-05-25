"""Pipeline-observability sub-router.

Extracted from ``routers/admin_digest.py``.  These are independent
read-only endpoints that delegate to sibling modules — none of them are
part of the catch-up digest itself.

The sub-router is mounted by ``admin_digest`` so all public paths
(e.g. ``/api/admin/regression-dispatcher/health``) remain unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.admin import _get_user_project_with_role
from app.auth import get_current_user, require_admin
from app.database import get_session
from app.models import (
    Alert,
    AlertSeverity,
    MaxPlanAccount,
    Project,
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskStatus,
    _utcnow,
)
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

logger = logging.getLogger(__name__)

# Matches the constant in admin_digest that pipeline-regressions reads.
_SIGNAL_STALENESS_TITLE_PREFIX = "Signal staleness: "

router = APIRouter()


@router.get("/provider-cost-summary")
async def get_provider_cost_summary(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate per-provider/account cost and success metrics for a rolling window.

    Groups terminal tasks by the account they ran on (stored in task.context),
    computing: total tasks, success rate, total spend, cost per merged PR.
    Returns rows sorted by total spend descending.
    """
    window_start = _utcnow() - timedelta(hours=hours)
    filters = [
        Task.created_at >= window_start,
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
    ]
    if project_id:
        filters.append(Task.project_id == project_id)

    tasks = (
        (
            await session.execute(
                select(Task)
                .where(*filters)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.vm_spot,
                        Task.vm_provisioned_at,
                        Task.vm_stopped_at,
                    )
                )
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )

    def _acct_id(ctx: Dict[str, Any] | None) -> str | None:
        if not ctx:
            return None
        return ctx.get("llm_account_id") or ctx.get("max_plan_account_id")

    acct_ids = {a for t in tasks if (a := _acct_id(t.context))}
    acct_map: Dict[str, Dict[str, str]] = {}
    if acct_ids:
        parsed = [uuid.UUID(a) for a in acct_ids if a]
        if parsed:
            rows = (
                (
                    await session.execute(
                        select(MaxPlanAccount).where(MaxPlanAccount.id.in_(parsed))
                    )
                )
                .scalars()
                .all()
            )
            for a in rows:
                acct_map[str(a.id)] = {"name": a.name, "provider": a.provider.value}

    groups: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        acct_id = _acct_id(t.context)
        info = acct_map.get(acct_id or "", {"name": "unknown", "provider": "unknown"})
        key = f"{info['provider']}|{acct_id or 'untracked'}"
        g = groups.setdefault(
            key,
            {
                "provider": info["provider"],
                "account_id": acct_id,
                "account_name": info["name"],
                "total_tasks": 0,
                "success_count": 0,
                "merged_count": 0,
                "total_cost_usd": 0.0,
            },
        )
        g["total_tasks"] += 1
        if t.status == TaskStatus.COMPLETED:
            g["success_count"] += 1
            if (t.context or {}).get("pr_merged"):
                g["merged_count"] += 1
        if t.vm_provisioned_at and t.vm_stopped_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            g["total_cost_usd"] += (
                t.vm_stopped_at - t.vm_provisioned_at
            ).total_seconds() * rate

    result_rows = []
    for g in groups.values():
        result_rows.append(
            {
                **g,
                "total_cost_usd": round(g["total_cost_usd"], 2),
                "success_rate": round(g["success_count"] / g["total_tasks"], 3)
                if g["total_tasks"]
                else None,
                "cost_per_merged_pr": round(g["total_cost_usd"] / g["merged_count"], 2)
                if g["merged_count"]
                else None,
            }
        )
    result_rows.sort(key=lambda x: x["total_cost_usd"], reverse=True)
    return {"window_hours": hours, "rows": result_rows}


@router.get("/drift-snapshot")
async def get_drift_snapshot(
    project_id: uuid.UUID,
    window_days: int = Query(7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Return per-direction drift signals for a project.

    Calls compute_direction_drift on-demand and returns the flagged directions
    (over_claim or stalled_despite_churn) plus the per-system rollup. The UI
    uses this to surface claim-vs-shipped divergence without requiring operators
    to understand the raw signal format.
    """
    from app.drift_detection import compute_direction_drift, last_n_drift_snapshots  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        return {
            "flagged_directions": [],
            "flagged_count": 0,
            "total_directions": 0,
            "any_system_flagged": False,
            "by_system": {},
            "generated_at": _utcnow().isoformat(),
            "window_days": window_days,
        }

    snapshot = await compute_direction_drift(session, project, window_days=window_days)
    direction_map = {
        d["id"]: d.get("text", "") for d in (project.directions or []) if d.get("id")
    }

    _SPARKLINE_N = 7
    flagged = []
    for did, sig in snapshot["by_direction"].items():
        if not (sig.get("over_claim_flag") or sig.get("stalled_despite_churn_flag")):
            continue
        raw_snaps = await last_n_drift_snapshots(
            session, project.id, did, n=_SPARKLINE_N
        )
        # Reverse to chronological order for sparkline rendering (oldest first).
        history = [
            {
                "snapshot_at": s.snapshot_at.isoformat(),
                "claim_shipped_consistency": s.claim_shipped_consistency,
                "over_claim_flag": s.over_claim_flag,
                "stalled_despite_churn_flag": s.stalled_despite_churn_flag,
            }
            for s in reversed(raw_snaps)
        ]
        flagged.append(
            {
                "direction_id": did,
                "direction_text": direction_map.get(did, ""),
                "over_claim_flag": sig["over_claim_flag"],
                "stalled_despite_churn_flag": sig["stalled_despite_churn_flag"],
                "claimed_changes_count": sig["claimed_changes_count"],
                "actually_merged_prs": sig["actually_merged_prs"],
                "claim_shipped_consistency": sig["claim_shipped_consistency"],
                "drift_history": history,
            }
        )

    return {
        "flagged_directions": flagged,
        "flagged_count": len(flagged),
        "total_directions": len(snapshot["by_direction"]),
        "any_system_flagged": any(
            s.get("over_claim_flag") or s.get("stalled_despite_churn_flag")
            for s in snapshot["by_system"].values()
        ),
        "by_system": snapshot["by_system"],
        "generated_at": snapshot["generated_at"],
        "window_days": snapshot["window_days"],
    }


@router.get("/fix-loop-convergence")
async def get_fix_loop_convergence(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate fix-loop telemetry across recently-completed/failed tasks.

    Returns per-loop average iterations, convergence rate, divergence reason
    histogram, and iteration-count distribution suitable for the dashboard
    convergence widget.  Also returns ``tasks_abandoned_on_divergence`` (int)
    and ``recent_abandoned_task_ids`` (list, capped at 25) for drill-in.
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.context,
            )
        )
        .order_by(Task.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    _LIMIT = 1000
    result = await session.execute(stmt.limit(_LIMIT))
    tasks = result.scalars().all()
    if len(tasks) >= _LIMIT:
        logger.warning(
            "get_fix_loop_convergence: query hit %d-row cap (hours=%d, project_id=%s); "
            "results may be incomplete",
            _LIMIT,
            hours,
            project_id,
        )

    # Only consider tasks that have at least one fix-loop event recorded.
    tasks_with_history = [t for t in tasks if t.fix_loop_history]

    # Abandoned-on-divergence counters (additive — do not affect per-loop aggs).
    abandoned_ids: List[str] = []
    for t in tasks:
        if (t.context or {}).get("fix_loop_abandoned"):
            abandoned_ids.append(str(t.id))
    abandoned_ids_recent = abandoned_ids[
        :25
    ]  # keep 25 most recent (DESC order: tasks are newest-first)

    loop_names = ("lint", "test", "check")

    # Per-loop bucket: list of (iterations, exit_reason) tuples
    per_loop: Dict[str, List[tuple]] = {loop: [] for loop in loop_names}

    for task in tasks_with_history:
        history = task.fix_loop_history or {}
        for loop in loop_names:
            bucket = history.get(loop) or []
            if not bucket:
                continue
            iterations = len(bucket)
            # Terminal exit reason is the last non-in_progress entry, else last entry.
            last_exit: Optional[str] = None
            for ev in reversed(bucket):
                reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                if reason and reason != "in_progress":
                    last_exit = reason
                    break
            if last_exit is None and bucket:
                last_exit = (
                    bucket[-1].get("exit_reason")
                    if isinstance(bucket[-1], dict)
                    else None
                )
            per_loop[loop].append((iterations, last_exit))

    def _aggregate_loop(entries: List[tuple]) -> Dict[str, Any]:
        if not entries:
            return {
                "task_count": 0,
                "avg_iterations": None,
                "convergence_rate": None,
                "exit_reason_counts": {},
                "iteration_distribution": {},
            }
        total_tasks = len(entries)
        total_iterations = sum(n for n, _ in entries)
        avg_iterations = round(total_iterations / total_tasks, 2)

        converged = sum(1 for _, r in entries if r == "passed")
        convergence_rate = round(converged / total_tasks, 3)

        exit_reason_counts: Dict[str, int] = {}
        for _, reason in entries:
            key = reason or "unknown"
            exit_reason_counts[key] = exit_reason_counts.get(key, 0) + 1

        # Iteration distribution: bucket by count (1, 2, 3, 4+)
        dist: Dict[str, int] = {"1": 0, "2": 0, "3": 0, "4+": 0}
        for n, _ in entries:
            if n == 1:
                dist["1"] += 1
            elif n == 2:
                dist["2"] += 1
            elif n == 3:
                dist["3"] += 1
            else:
                dist["4+"] += 1

        return {
            "task_count": total_tasks,
            "avg_iterations": avg_iterations,
            "convergence_rate": convergence_rate,
            "exit_reason_counts": exit_reason_counts,
            "iteration_distribution": dist,
        }

    return {
        "window_hours": hours,
        "tasks_scanned": len(tasks),
        "tasks_with_fix_loops": len(tasks_with_history),
        "tasks_abandoned_on_divergence": len(abandoned_ids),
        "recent_abandoned_task_ids": abandoned_ids_recent,
        "lint": _aggregate_loop(per_loop["lint"]),
        "test": _aggregate_loop(per_loop["test"]),
        "check": _aggregate_loop(per_loop["check"]),
    }


@router.get("/fix-loop-convergence-trend")
async def get_fix_loop_convergence_trend(
    days: int = Query(7, ge=1, le=30),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return per-day buckets of fix-loop convergence and divergence rates.

    Each bucket contains, for lint/test/check loops:
      - task_count: tasks with that loop type
      - convergence_rate: fraction that exited with 'passed'
      - divergence_rate: fraction that exited with a divergence_* reason
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    _TREND_LIMIT = 10_000

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.created_at,
            )
        )
        .limit(_TREND_LIMIT)
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    result = await session.execute(stmt)
    tasks = result.scalars().all()
    if len(tasks) >= _TREND_LIMIT:
        logger.warning(
            "[ANALYTICS] fix_loop_convergence_trend hit row cap (%d); results may be incomplete. "
            "Consider reducing days= or adding a project_id filter.",
            _TREND_LIMIT,
        )

    loop_names = ("lint", "test", "check")

    # day_str -> loop -> list of exit_reasons
    day_buckets: Dict[str, Dict[str, List[Optional[str]]]] = defaultdict(
        lambda: {loop: [] for loop in loop_names}
    )

    for task in tasks:
        history = task.fix_loop_history or {}
        if not history:
            continue
        created = task.created_at
        if created is None:
            continue
        day_str = created.strftime("%Y-%m-%d")
        for loop in loop_names:
            bucket = history.get(loop) or []
            if not bucket:
                continue
            last_exit: Optional[str] = None
            for ev in reversed(bucket):
                reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                if reason and reason != "in_progress":
                    last_exit = reason
                    break
            if last_exit is None and bucket:
                last_exit = (
                    bucket[-1].get("exit_reason")
                    if isinstance(bucket[-1], dict)
                    else None
                )
            day_buckets[day_str][loop].append(last_exit)

    def _day_loop_stats(reasons: List[Optional[str]]) -> Dict[str, Any]:
        n = len(reasons)
        if n == 0:
            return {"task_count": 0, "convergence_rate": None, "divergence_rate": None}
        converged = sum(1 for r in reasons if r == "passed")
        diverged = sum(1 for r in reasons if r and r.startswith("divergence_"))
        return {
            "task_count": n,
            "convergence_rate": round(converged / n, 3),
            "divergence_rate": round(diverged / n, 3),
        }

    # Build sorted list covering every day in the window
    buckets = []
    for i in range(days - 1, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        entry: Dict[str, Any] = {"date": day}
        day_data = day_buckets.get(day, {ln: [] for ln in loop_names})
        for loop in loop_names:
            entry[loop] = _day_loop_stats(day_data.get(loop, []))
        buckets.append(entry)

    if project_id is not None:
        try:
            from app.playbook_learning import check_fix_loop_divergence_regression  # noqa: PLC0415

            await check_fix_loop_divergence_regression(project_id, session)
        except Exception:
            logger.exception(
                "[ANALYTICS] fix_loop_divergence_regression check failed (non-critical)"
            )

    return {
        "days": days,
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# Fix-loop diverged-task drill-down endpoint
# ---------------------------------------------------------------------------


@router.get("/fix-loop-diverged-tasks")
async def get_fix_loop_diverged_tasks(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    loop: str | None = Query(
        None, description="Filter by loop name: lint, test, or check"
    ),
    exit_reason: str | None = Query(
        None, description="Filter by exit reason, e.g. divergence_oscillation"
    ),
    limit: int = Query(50, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List tasks that exited a fix loop with a specific reason in the given window.

    Returns up to `limit` tasks ordered by most-recent first, with enough
    context (title, type, scope, project, exit reason, iteration count,
    error_tail) for an operator to decide which task to investigate next.
    """
    if loop is not None and loop not in ("lint", "test", "check"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid loop '{loop}'. Must be one of: lint, test, check.",
        )

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.title,
                Task.status,
                Task.fix_loop_history,
                Task.context,
                Task.payloads,
                Task.created_at,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(5000)
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    result = await session.execute(stmt)
    tasks = result.scalars().all()

    loop_names = [loop] if loop is not None else ["lint", "test", "check"]

    rows = []
    for task in tasks:
        if len(rows) >= limit:
            break
        history = task.fix_loop_history or {}
        for lname in loop_names:
            bucket = history.get(lname) or []
            if not bucket:
                continue
            # Resolve terminal exit reason (last non-in_progress event).
            last_exit: Optional[str] = None
            last_error_tail: Optional[str] = None
            for ev in reversed(bucket):
                if not isinstance(ev, dict):
                    continue
                reason = ev.get("exit_reason")
                if reason and reason != "in_progress":
                    last_exit = reason
                    last_error_tail = ev.get("error_tail")
                    break
            if exit_reason is not None and last_exit != exit_reason:
                continue
            if last_exit is None or last_exit == "passed":
                continue
            ctx = task.context or {}
            rows.append(
                {
                    "task_id": str(task.id),
                    "title": task.title or "Untitled",
                    "task_type": (task.first_payload or {}).get("type", "unknown"),
                    "scope": ctx.get("scope"),
                    "project_id": str(task.project_id),
                    "status": task.status,
                    "loop": lname,
                    "exit_reason": last_exit,
                    "iteration_count": len(bucket),
                    "error_tail": last_error_tail,
                }
            )
            if len(rows) >= limit:
                break

    return {
        "window_hours": hours,
        "loop_filter": loop,
        "exit_reason_filter": exit_reason,
        "total": len(rows),
        "tasks": rows,
    }


@router.get("/pipeline-regressions")
async def get_pipeline_regressions(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate active pipeline regression signals with autonomous loop response status.

    Reads flags already written by the existing detectors (sr_catch_rate_trends_alert,
    fix_loop_divergence_alerted_window, Alert table) — does not re-implement detection.
    Returns an empty regressions list when the system is healthy. Field names match
    the PipelineRegression interface consumed by admin-ui PipelineRegressionsPanel.
    """
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    doc = project.design_doc or {}
    now = datetime.utcnow()
    regressions: List[Dict[str, Any]] = []

    # SR catch-rate regression signals — regression_class uses the colon form the
    # dispatcher actually persists (pipeline_regression_dispatcher.py:306).
    sr_alert = doc.get("sr_catch_rate_trends_alert") or {}
    for cat in list(sr_alert.get("categories") or []):
        cat_data = (
            (doc.get("sr_catch_rate_trends") or {}).get("categories") or {}
        ).get(cat) or {}
        regressions.append(
            {
                "signal_name": f"SR catch-rate regression: {cat}",
                "regression_class": f"sr_catch_rate:{cat}",
                "detected_at": sr_alert.get("computed_at"),
                "severity": "warning",
                "current_rate": cat_data.get("current_hit_rate"),
                "prior_rate": cat_data.get("previous_hit_rate"),
                "resolved": False,
                "resolved_at": None,
            }
        )

    # Fix-loop divergence regression signals — colon form per dispatcher line 394.
    iso_year, iso_week, _ = now.isocalendar()
    if doc.get("fix_loop_divergence_alerted_window") == f"{iso_year}-W{iso_week:02d}":
        for reg in list(doc.get("fix_loop_divergence_regressions") or []):
            loop = reg.get("loop", "unknown")
            regressions.append(
                {
                    "signal_name": f"Fix-loop divergence regression: {loop} loop",
                    "regression_class": f"fix_loop_divergence:{loop}",
                    "detected_at": doc.get("fix_loop_divergence_detected_at"),
                    "severity": "warning",
                    "current_rate": reg.get("current_rate"),
                    "prior_rate": reg.get("prior_rate"),
                    "resolved": False,
                    "resolved_at": None,
                }
            )

    # Background service crash signals — undismissed ERROR alerts in last 48h.
    crash_cutoff = now - timedelta(hours=48)
    crash_rows = (
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.created_at >= crash_cutoff,
                    Alert.dismissed_at.is_(None),
                )
                .order_by(Alert.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    _CRASH_TITLE_PREFIX = "Background service crashed: "
    for alert in crash_rows:
        if alert.title and alert.title.startswith(_CRASH_TITLE_PREFIX):
            service_name = alert.title[len(_CRASH_TITLE_PREFIX) :].strip() or "unknown"
            reg_class = f"background_service_crash:{service_name}"
        else:
            reg_class = "background_service_crash"
        regressions.append(
            {
                "signal_name": alert.title,
                "regression_class": reg_class,
                "detected_at": alert.created_at.isoformat()
                if alert.created_at
                else None,
                "severity": "critical",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
            }
        )

    staleness_rows = (
        (
            await session.execute(
                select(Alert)
                .where(
                    Alert.project_id == project_id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.created_at >= now - timedelta(days=14),
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SIGNAL_STALENESS_TITLE_PREFIX}%"),
                )
                .order_by(Alert.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for alert in staleness_rows:
        signal_name = alert.title[len(_SIGNAL_STALENESS_TITLE_PREFIX) :].strip()
        regressions.append(
            {
                "signal_name": alert.title,
                "regression_class": f"signal_staleness:{signal_name or 'unknown'}",
                "detected_at": alert.created_at.isoformat()
                if alert.created_at
                else None,
                "severity": "warning",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
                "details": alert.message,
            }
        )

    # Viewport-regression cases — active RegressionRemediationCase rows whose
    # regression_class starts with "viewport_regression:". Sourced from the durable
    # table written by the pipeline_regression_dispatcher; no design_doc flags needed.
    viewport_cutoff = now - timedelta(days=30)
    viewport_rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.regression_class.like(
                        "viewport_regression:%"
                    ),
                    RegressionRemediationCase.status.in_(
                        [
                            RegressionCaseStatus.ACTIVE,
                            RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                        ]
                    ),
                    RegressionRemediationCase.first_seen_at >= viewport_cutoff,
                )
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for case in viewport_rows:
        subkey = case.regression_class.split(":", 1)[-1]
        regressions.append(
            {
                "signal_name": f"Viewport regression: {subkey}",
                "regression_class": case.regression_class,
                "detected_at": case.first_seen_at.isoformat()
                if case.first_seen_at
                else None,
                "severity": "warning",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
                "introducing_pr_number": getattr(case, "introducing_pr_number", None),
                "introducing_commit_sha": getattr(case, "introducing_commit_sha", None),
            }
        )

    # Enrich each regression with the status of any spawned investigation task.
    # Includes both pipeline_regression_dispatcher tasks (SR/fix-loop classes) and
    # crash_investigation tasks spawned by lifecycle._spawn_crash_investigations.
    if regressions:
        dispatcher_tasks = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project_id,
                        Task.context["spawned_by"]
                        .as_string()
                        .in_(("pipeline_regression_dispatcher", "crash_investigation")),
                        Task.created_at >= now - timedelta(hours=48),
                    )
                    .options(load_only(Task.id, Task.title, Task.status, Task.context))
                    .order_by(Task.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        by_class: Dict[str, Task] = {}
        for t in dispatcher_tasks:
            rc = (t.context or {}).get("regression_class")
            if rc and rc not in by_class:
                by_class[rc] = t
        for r in regressions:
            task = by_class.get(r["regression_class"])
            if task is None:
                r["spawned_task_id"] = None
                r["spawned_task_status"] = None
                r["spawned_task_title"] = None
            else:
                r["spawned_task_id"] = str(task.id)
                r["spawned_task_status"] = (
                    task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status)
                )
                r["spawned_task_title"] = task.title

    return {
        "project_id": str(project_id),
        "regressions": regressions,
        "healthy": len(regressions) == 0,
    }


@router.get("/regression-dispatcher/health")
async def get_regression_dispatcher_health(
    project_id: Optional[uuid.UUID] = Query(default=None),
    window_hours: int = Query(default=24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Dispatcher health and throughput summary for the last N hours.

    Returns aggregate cycle counts, per-hour spawn sparkline, and per-class
    last-spawn/cooldown info derived from the durable
    ``DispatcherCycleRecord`` table and the ``RegressionRemediationCase``
    table.  Works at all viewport widths and returns zero / empty arrays on
    a cold start (empty table) — never raises — so the UI shows 'no data'
    rather than crashing.
    """
    from app.dispatcher_read_api import get_dispatcher_cycle_records  # noqa: PLC0415
    from app.regression_dispatcher_metrics import compute_mtti_p50_sec  # noqa: PLC0415

    pid_str = str(project_id) if project_id else None
    # ``window_hours`` is bounded to 168 above; pull a large enough page that
    # the aggregates and sparkline both cover the full window.  Default tick
    # interval is 60s → 168h * 60 = ~10080 rows worst case; we cap at 11000
    # to cover the last week at 1-minute resolution without truncation.  The
    # (project_id, cycle_started_at) index makes this read cheap.
    cycles = await get_dispatcher_cycle_records(
        session,
        project_id=pid_str,
        limit=11000,
        window_hours=window_hours,
    )

    # Aggregate totals
    total_cycles = len(cycles)
    total_signals_evaluated = sum(r.get("signals_evaluated", 0) for r in cycles)
    total_signals_tripped = sum(r.get("signals_tripped", 0) for r in cycles)
    total_spawns = sum(r.get("spawns_emitted", 0) for r in cycles)
    total_cooldown = sum(
        r.get("rejections_by_reason", {}).get("cooldown", 0) for r in cycles
    )
    total_dedup = sum(r.get("rejections_by_reason", {}).get("dedup", 0) for r in cycles)
    total_cap = sum(
        r.get("rejections_by_reason", {}).get("system_cap", 0) for r in cycles
    )
    total_mi_suppressed = sum(
        r.get("rejections_by_reason", {}).get("manual_intervention_suppressed", 0)
        for r in cycles
    )

    # Compute mttd_p50_sec as mean of non-None values
    mttd_values = [
        r.get("mttd_p50_sec") for r in cycles if r.get("mttd_p50_sec") is not None
    ]
    mttd_p50_sec = sum(mttd_values) / len(mttd_values) if mttd_values else None

    # Sum recovery counts across cycles
    total_marginal_recoveries = sum(
        r.get("marginal_recoveries_count", 0) for r in cycles
    )
    total_confirmed_recoveries = sum(
        r.get("confirmed_recoveries_count", 0) for r in cycles
    )

    # Per-hour sparkline: count spawns_emitted in each 1-hour bucket
    now = datetime.utcnow()
    sparkline: List[Dict[str, Any]] = []
    for h in range(window_hours - 1, -1, -1):
        bucket_start = (now - timedelta(hours=h + 1)).isoformat()
        bucket_end = (now - timedelta(hours=h)).isoformat()
        bucket_spawns = sum(
            r.get("spawns_emitted", 0)
            for r in cycles
            if bucket_start <= r.get("cycle_started_at", "") < bucket_end
        )
        bucket_cycles = sum(
            1
            for r in cycles
            if bucket_start <= r.get("cycle_started_at", "") < bucket_end
        )
        sparkline.append(
            {
                "hour_start": bucket_start,
                "spawns": bucket_spawns,
                "cycles": bucket_cycles,
            }
        )

    # Per-class info from DB: last seen_at, status, cooldown
    where_clause = [RegressionRemediationCase.status != RegressionCaseStatus.CLOSED]
    if project_id is not None:
        where_clause.append(RegressionRemediationCase.project_id == project_id)
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where_clause)
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    from app.pipeline_regression_dispatcher import _REGRESSION_COOLDOWN  # noqa: PLC0415

    # Compute MTTI: query cases acknowledged within the window
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    mtti_where = [
        RegressionRemediationCase.operator_acknowledged_at >= cutoff,
        RegressionRemediationCase.needs_manual_intervention_at.is_not(None),
    ]
    if project_id is not None:
        mtti_where.append(RegressionRemediationCase.project_id == project_id)
    acked_cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(*mtti_where).limit(1000)
            )
        )
        .scalars()
        .all()
    )
    mtti_p50_sec = compute_mtti_p50_sec(list(acked_cases))

    per_class: List[Dict[str, Any]] = []
    for case in cases:
        last_seen = case.last_seen_at
        cooldown_until: Optional[str] = None
        if last_seen:
            cooldown_end = last_seen + _REGRESSION_COOLDOWN
            if cooldown_end > now:
                cooldown_until = cooldown_end.isoformat()
        per_class.append(
            {
                "project_id": str(case.project_id),
                "regression_class": case.regression_class,
                "status": case.status,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "attempts": case.attempts,
                "in_cooldown_until": cooldown_until,
            }
        )

    return {
        "window_hours": window_hours,
        "aggregate": {
            "cycles": total_cycles,
            "signals_evaluated": total_signals_evaluated,
            "signals_tripped": total_signals_tripped,
            "spawns": total_spawns,
            "mttd_p50_sec": mttd_p50_sec,
            "mtti_p50_sec": mtti_p50_sec,
            "recoveries": {
                "marginal": total_marginal_recoveries,
                "confirmed": total_confirmed_recoveries,
            },
            "rejections": {
                "cooldown": total_cooldown,
                "dedup": total_dedup,
                "system_cap": total_cap,
                "manual_intervention_suppressed": total_mi_suppressed,
            },
        },
        "spawns_sparkline": sparkline,
        "per_class": per_class,
    }


@router.get("/regression-dispatcher/mtta")
async def get_regression_mtta(
    project_id: Optional[uuid.UUID] = Query(default=None),
    window_days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """MTTA (Mean-Time-to-Auto-Fix) per regression class.

    Returns p50/p90 lifecycle stage durations from historical cases and live
    elapsed times for currently-active cases so operators can see 'is this
    auto-fixing on schedule or has it stalled?'
    """
    from app.dispatcher_read_api import (  # noqa: PLC0415
        get_regression_mtta_dashboard_data,
    )

    return await get_regression_mtta_dashboard_data(
        session,
        project_id=project_id,
        window_days=window_days,
    )


@router.get("/projects/{project_id}/warm-reuse-funnel")
async def get_warm_reuse_funnel(
    project_id: uuid.UUID,
    window_hours: int = Query(default=24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Rolling warm-VM backlog reuse funnel counters for the last ``window_hours``.

    Returns stage counters for the warm-reuse pipeline so the bottleneck is
    identifiable without changing claim behavior:

    - ``completed_vms_with_eligible_backlog``: COMPLETION events that found ≥1
      eligible QUEUED backlog task.
    - ``teardown_defers_scheduled``: VMs that entered the teardown-defer window.
    - ``next_role_polls_during_defer``: /next-role calls inside the defer window.
    - ``warm_claims_succeeded``: polls that claimed a task (vm_warm_start=True).
    - ``claim_skipped_by_reason``: dict of reason→count for missed claims.
    - ``claim_skipped_total``: total missed claims.

    Counters are in-memory and reset on instance restart.
    """
    from app.warm_backlog_reuse import get_reuse_funnel_snapshot  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return get_reuse_funnel_snapshot(window_hours=window_hours)


@router.get("/regression-dispatcher/escalation-outcomes")
async def get_regression_escalation_outcomes(
    project_id: Optional[uuid.UUID] = Query(default=None),
    lookback_days: int = Query(default=30, ge=1, le=180),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate case outcomes by escalation_reason bucket.

    Groups RegressionRemediationCase rows into six buckets: four meta-escalation
    reasons (escalated_cross_layer, escalated_layer_concentration,
    escalated_cross_case_fingerprint, escalated_recurrence), one
    compliance_followup bucket for cross-layer cases where a followup fix was
    spawned and merged, and all_other.  Returns per-bucket merged_count,
    retripped_within_30d_count, durable_resolution_rate, median_hold_days,
    prevention_rate_30d, recurrences_observed, and in_flight_excluded_count.
    Cross-layer cases with a compliance followup fix are re-bucketed into
    compliance_followup so operators can compare the loop's lift against baseline.
    """
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_escalation_outcome_breakdown,
    )

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    where: List = [RegressionRemediationCase.first_seen_at >= cutoff]
    if project_id is not None:
        where.append(RegressionRemediationCase.project_id == project_id)

    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(*where)
                .order_by(RegressionRemediationCase.first_seen_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    return compute_escalation_outcome_breakdown(
        list(cases), lookback_days=lookback_days
    )


@router.get("/pipeline-efficiency")
async def get_pipeline_efficiency(
    project_id: Optional[uuid.UUID] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate VM cold-start latency, warm reuse rate, and review iterations.

    Returns p50/p95 of cold-start setup_secs, warm VM reuse fraction, and
    review-iteration median/p95 for the last 24h and 7d windows, each with a
    14-day-prior delta for trend comparison.
    """
    now = _utcnow()
    filters = [
        Task.created_at >= now - timedelta(days=21),
        Task.status == TaskStatus.COMPLETED,
    ]
    if project_id:
        filters.append(Task.project_id == project_id)
    tasks = (
        (
            await session.execute(
                select(Task)
                .where(*filters)
                .options(load_only(Task.id, Task.status, Task.context, Task.created_at))
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )

    def _pct(values: list, p: int):
        if not values:
            return None
        s = sorted(values)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    def _metrics(bucket: list) -> Dict[str, Any]:
        timing_list = [(t.context or {}).get("_timing") or {} for t in bucket]
        cold = [
            float(tm["setup_secs"])
            for tm in timing_list
            if isinstance(tm.get("setup_secs"), (int, float))
            and not tm.get("warm_start")
        ]
        warm_n = sum(1 for tm in timing_list if tm.get("warm_start") is True)
        timed_n = sum(1 for tm in timing_list if tm)
        iters = [
            int((t.context or {}).get("review_loop", {}).get("iteration") or 0)
            for t in bucket
            if (t.context or {}).get("review_loop")
        ]
        return {
            "setup_p50_secs": _pct(cold, 50),
            "setup_p95_secs": _pct(cold, 95),
            "setup_sample_n": len(cold),
            "warm_reuse_rate": round(warm_n / timed_n, 3) if timed_n else None,
            "warm_reuse_sample_n": timed_n,
            "review_iter_median": _pct(iters, 50),
            "review_iter_p95": _pct(iters, 95),
            "review_iter_sample_n": len(iters),
        }

    def _in(start, end):
        return [t for t in tasks if start <= (t.created_at or now) < end]

    prior_end = now - timedelta(days=14)
    cur_24h = _metrics(_in(now - timedelta(hours=24), now))
    cur_7d = _metrics(_in(now - timedelta(days=7), now))
    pri_24h = _metrics(_in(now - timedelta(hours=24) - timedelta(days=14), prior_end))
    pri_7d = _metrics(_in(now - timedelta(days=21), prior_end))

    def _delta(a, b, k):
        av, bv = a.get(k), b.get(k)
        return round(av - bv, 4) if av is not None and bv is not None else None

    def _augment(cur, pri):
        return {
            **cur,
            "vs_prior_setup_p50_delta": _delta(cur, pri, "setup_p50_secs"),
            "vs_prior_warm_reuse_rate_delta": _delta(cur, pri, "warm_reuse_rate"),
            "vs_prior_review_iter_median_delta": _delta(cur, pri, "review_iter_median"),
        }

    return {
        "computed_at": now.isoformat(),
        "window_24h": _augment(cur_24h, pri_24h),
        "window_7d": _augment(cur_7d, pri_7d),
    }


@router.get("/canonical-regression-layers")
async def get_canonical_regression_layers(
    current_user: dict = Depends(get_current_user),  # noqa: ARG001
):
    """Return the canonical regression layers with descriptions and display order."""
    from app.regression_layer_classifier import (  # noqa: PLC0415
        LAYER_DISPLAY_ORDER,
        LAYER_DESCRIPTIONS,
    )

    return {
        "layers": [
            {
                "name": layer,
                "description": LAYER_DESCRIPTIONS.get(layer, ""),
            }
            for layer in LAYER_DISPLAY_ORDER
        ]
    }
