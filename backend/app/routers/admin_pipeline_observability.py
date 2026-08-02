"""Pipeline-observability sub-router.

Extracted from ``routers/admin_digest.py``.  These are independent
read-only endpoints that delegate to sibling modules — none of them are
part of the catch-up digest itself.

The sub-router is mounted by ``admin_digest`` so all surviving public paths
(e.g. ``/api/admin/pipeline-regressions``) remain unchanged. The
``/regression-dispatcher/*`` and ``/canonical-regression-layers`` routes were
removed alongside the detect->investigate->fix regression apparatus.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, delete, func
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.admin_helpers import _get_user_project_with_role
from app.auth import get_current_user, get_metrics_reader, require_admin
from app.database import get_session, resolve_dialect_name
from app.sr_analytics_store import read_project_sr_analytics
from app.failure_layer_classifier import parse_viewport_regression_class
from app.models import (
    Alert,
    AlertSeverity,
    MaxPlanAccount,
    Project,
    Task,
    TaskStatus,
    UiLoadTimingBaseline,
    UiLoadTimingSample,
    _utcnow,
    get_task_total_vm_cost_usd_or_none,
)
from app.vm_cost_rates import vm_cost_sql_expr

logger = logging.getLogger(__name__)

# Matches the Alert title written by the pipeline-alert scan
# (``pipeline_alert_scan._run_signal_staleness_watchdog``), the re-homed source
# of the "Signal staleness" WARNING alerts surfaced by ``/pipeline-regressions``.
_SIGNAL_STALENESS_TITLE_PREFIX = "Signal staleness: "
_VIEWPORT_FAILURE_CLASS = "VIEWPORT_REGRESSION"
_VIEWPORT_TASK_LOOKBACK_DAYS = 14
_VIEWPORT_ASSERTION_FUNCTIONS = {
    "expectNoOverflowAt": "overflow",
    "expectNoTextTruncationAt": "text_truncation",
    "expectMinTouchTargetAt": "touch_target",
}
_VIEWPORT_ASSERTION_RE = re.compile(
    r"\b(expectNoOverflowAt|expectNoTextTruncationAt|expectMinTouchTargetAt)\((\d+)\)"
)
_VIEWPORT_TEST_FILE_RE = re.compile(r"\[?([A-Za-z0-9_.-]+)\.viewport\.test\.tsx\]?")
_VIEWPORT_OVERFLOW_COMPONENT_RE = re.compile(
    r"[:\s]([A-Za-z][A-Za-z0-9_.-]+)\s+overflows\s+viewport\b",
    re.IGNORECASE,
)
_VIEWPORT_IN_COMPONENT_RE = re.compile(
    r"\bin\s+([A-Za-z][A-Za-z0-9_.-]+)(?:\b|$)",
    re.IGNORECASE,
)

router = APIRouter()

# Separate router for endpoints that accept agent JWTs via get_metrics_reader.
# Registered directly in main.py WITHOUT the get_current_user router-level dep
# so agent JWTs can reach these endpoints. All routes here MUST be read-only.
metrics_router = APIRouter(
    prefix="/api/admin",
    tags=["admin-metrics"],
)


def _iter_viewport_regression_keys(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.startswith("viewport_regression:"):
                yield key
            yield from _iter_viewport_regression_keys(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_viewport_regression_keys(item)


def _viewport_metadata_from_regression_class(
    regression_class: str,
) -> Dict[str, Any] | None:
    parsed = parse_viewport_regression_class(regression_class)
    component = parsed.get("component")
    width = parsed.get("width")
    if not component or not width:
        return None
    try:
        numeric_width = int(width)
    except (TypeError, ValueError):
        return None
    if numeric_width <= 0:
        return None
    return {
        "component": component,
        "assertion_type": parsed.get("assertion_type"),
        "width": numeric_width,
    }


def _component_from_viewport_status_text(message: str) -> str | None:
    if match := _VIEWPORT_TEST_FILE_RE.search(message):
        return match.group(1)
    if match := _VIEWPORT_OVERFLOW_COMPONENT_RE.search(message):
        return match.group(1)
    if match := _VIEWPORT_IN_COMPONENT_RE.search(message):
        return match.group(1)
    return None


def _viewport_metadata_from_status_text(message: str | None) -> Dict[str, Any] | None:
    if not message:
        return None
    match = _VIEWPORT_ASSERTION_RE.search(message)
    if not match:
        return None
    assertion_func, width = match.groups()
    component = _component_from_viewport_status_text(message)
    if not component:
        return None
    return {
        "component": component,
        "assertion_type": _VIEWPORT_ASSERTION_FUNCTIONS[assertion_func],
        "width": int(width),
    }


def _viewport_failure_context(task: Task) -> tuple[bool, list[str]]:
    ctx = task.context or {}
    if not isinstance(ctx, dict):
        ctx = {}

    failure_class = str(
        ctx.get("failure_category") or ctx.get("failure_class") or ""
    ).strip()
    viewport_keys = list(_iter_viewport_regression_keys(ctx))
    return failure_class == _VIEWPORT_FAILURE_CLASS or bool(
        viewport_keys
    ), viewport_keys


def _viewport_metadata_for_failed_task(
    task: Task, viewport_keys: list[str]
) -> Dict[str, Any] | None:

    for key in viewport_keys:
        if metadata := _viewport_metadata_from_regression_class(key):
            return metadata
    return _viewport_metadata_from_status_text(task.latest_status_message)


def _assemble_provider_cost_rows(
    groups: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Finalize per-account cost rows (rounding, success_rate, cost_per_merged_pr).

    Shared by the SQL fast path and the SQLite fallback so both emit an
    identical row shape.
    """
    result_rows: List[Dict[str, Any]] = []
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
    return result_rows


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
    dialect_name = resolve_dialect_name(session)
    filters = [
        Task.vm_stopped_at >= window_start,
        Task.status.in_(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
    ]
    if project_id:
        filters.append(Task.project_id == project_id)

    groups: Dict[str, Dict[str, Any]] = {}

    if dialect_name == "postgresql":
        # Fast path: group by account and sum cost/counts in SQL so the
        # 5000-row scan (which hydrated the JSONB ``context`` of every terminal
        # task) is replaced by a single aggregate.  Account name/provider are
        # hydrated afterwards, and rows are assembled identically below.
        acct_sql = func.coalesce(
            Task.context["llm_account_id"].astext,
            Task.context["max_plan_account_id"].astext,
        )
        cost_sql = vm_cost_sql_expr(dialect_name)
        completed = Task.status == TaskStatus.COMPLETED
        pr_merged = Task.context["pr_merged"].astext == "true"
        agg_rows = (
            await session.execute(
                sa_select(
                    acct_sql.label("acct_id"),
                    func.count().label("total_tasks"),
                    func.sum(case((completed, 1), else_=0)).label("success_count"),
                    func.sum(
                        case((and_(completed, pr_merged), 1), else_=0)
                    ).label("merged_count"),
                    func.coalesce(func.sum(cost_sql), 0.0).label("total_cost_usd"),
                )
                .where(*filters)
                .group_by(acct_sql)
            )
        ).all()

        acct_ids = {r.acct_id for r in agg_rows if r.acct_id}
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
                    acct_map[str(a.id)] = {
                        "name": a.name,
                        "provider": a.provider.value,
                    }

        for r in agg_rows:
            acct_id = r.acct_id
            info = acct_map.get(
                acct_id or "", {"name": "unknown", "provider": "unknown"}
            )
            key = f"{info['provider']}|{acct_id or 'untracked'}"
            groups[key] = {
                "provider": info["provider"],
                "account_id": acct_id,
                "account_name": info["name"],
                "total_tasks": int(r.total_tasks),
                "success_count": int(r.success_count or 0),
                "merged_count": int(r.merged_count or 0),
                "total_cost_usd": float(r.total_cost_usd or 0.0),
            }

        result_rows = _assemble_provider_cost_rows(groups)
        result_rows.sort(key=lambda x: x["total_cost_usd"], reverse=True)
        return {"window_hours": hours, "rows": result_rows}

    # SQLite fallback (test harness without Postgres): compute per-row in Python.
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
                        Task.vm_runtime_cost_banked_db,
                    raiseload=True,)
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
        cost = get_task_total_vm_cost_usd_or_none(t)
        if cost is not None:
            g["total_cost_usd"] += cost

    result_rows = _assemble_provider_cost_rows(groups)
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
    cutoff = _utcnow() - timedelta(hours=hours)

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
            Task.fix_loop_history != {},
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.context,
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    _LIMIT = 200
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
    cutoff = _utcnow() - timedelta(days=days)

    _TREND_LIMIT = 2_000

    stmt = (
        select(Task)
        .where(
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.created_at >= cutoff,
            Task.fix_loop_history != {},
        )
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.fix_loop_history,
                Task.created_at,
            raiseload=True,)
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
        day = (_utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
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
# Outer review<->fix loop convergence (PR-level cycle, not inner lint/test/check)
# ---------------------------------------------------------------------------

_OUTER_LOOP_ESCALATION_PREFIX = "Review-fix escalation: "

# Query caps for the outer-loop endpoints. When a window can exceed these the
# aggregate silently under-counts, so each call site logs on overflow (project
# playbook: "no silent caps").
_OUTER_LOOP_TASK_CAP = 5000
_OUTER_LOOP_TREND_TASK_CAP = 10_000
_OUTER_LOOP_ALERT_CAP = 5000


async def _load_escalation_alerts(
    session: AsyncSession,
    *,
    cutoff: datetime,
    project_id: uuid.UUID | None,
) -> List[Alert]:
    """Load review-fix early-escalation Alerts created since ``cutoff``."""
    alert_stmt = (
        select(Alert)
        .where(
            Alert.created_at >= cutoff,
            Alert.title.like(f"{_OUTER_LOOP_ESCALATION_PREFIX}%"),
        )
        .options(load_only(Alert.id, Alert.title, Alert.task_id, Alert.created_at, raiseload=True))
        .limit(5000)
    )
    if project_id is not None:
        alert_stmt = alert_stmt.where(Alert.project_id == project_id)
    rows = list((await session.execute(alert_stmt)).scalars().all())
    if len(rows) >= _OUTER_LOOP_ALERT_CAP:
        logger.warning(
            "[OUTER-LOOP] escalation-alert query hit the %d-row cap (project=%s, "
            "cutoff=%s); convergence aggregate may under-count escalations",
            _OUTER_LOOP_ALERT_CAP,
            project_id,
            cutoff.isoformat(),
        )
    return rows


async def _load_outer_loop_original_tasks(
    session: AsyncSession,
    tasks: List[Task],
    *,
    project_id: uuid.UUID | None,
) -> Dict[str, Task]:
    """Load chain-original tasks referenced by in-window review loops.

    The aggregation window is based on the review-loop task timestamp, but the
    chain-original task can predate that window. We hydrate those originals so
    terminal outcomes stamped there remain visible to the reducer.
    """
    original_ids: set[str] = set()
    for task in tasks:
        rl = (task.context or {}).get("review_loop")
        if isinstance(rl, dict):
            oid = rl.get("original_task_id")
            if isinstance(oid, str) and oid:
                original_ids.add(oid)
    if not original_ids:
        return {}

    stmt = (
        select(Task)
        .where(Task.id.in_([uuid.UUID(oid) for oid in original_ids]))
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.context,
                Task.created_at,
                # The convergence reducer walks these via _derive_completed_at,
                # which reads the durable terminal timestamp; a deferred read
                # under raiseload=True raises, so load it eagerly.
                Task.completed_at,
                # Hydrated originals are appended to the task list the
                # convergence reducer walks, and an in-window original can be
                # CANCELLED (outside the COMPLETED/FAILED window query), so the
                # outcome walker (task_outcomes.evaluate_task_outcome) reads
                # first_payload -> payloads and, on the CANCELLED path,
                # direction_outcomes._is_cascade_cancelled reads
                # latest_status_message. Leaving either deferred lazy-loads
                # inside the async session and raises MissingGreenlet. The set
                # is small (one original per PR chain), so loading the columns
                # outright is fine — no projection needed.
                Task.payloads,
                Task.latest_status_message,
                # The lane-waste reducer (digest_metrics._lane_waste_task_row,
                # reached via compute_lane_waste_diagnostics) ALSO walks these
                # hydrated originals and reads more columns: fix_loop_history
                # (classify_lane_waste_category -> has_terminal_divergence),
                # started_at, status_log + updated_at (_lane_waste_completed_at),
                # and archived (task_counts_toward_review_cap). Leaving any
                # deferred lazy-loads inside the async session and raises
                # MissingGreenlet (prod 2026-06-13: /api/admin/lane-diag 500
                # storm — getattr(t, "fix_loop_history", None) does NOT suppress
                # the deferred load).
                Task.fix_loop_history,
                Task.started_at,
                Task.status_log,
                Task.updated_at,
                Task.archived,
            raiseload=True,)
        )
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    originals = list((await session.execute(stmt)).scalars().all())
    return {str(task.id): task for task in originals}


# Columns the outer-loop chain reducers AND the per-task outcome walker
# (task_outcomes.evaluate_task_outcome, reached via the convergence summary's
# non_code_outcomes block) read directly off each row. ``latest_status_message``
# is required by the walker's CANCELLED classification
# (direction_outcomes._is_cascade_cancelled); a deferred read of any of these
# inside the async session raises MissingGreenlet (prod 2026-06-07: 500s on
# /api/admin/digest and both outer-loop-convergence endpoints).
_OUTER_LOOP_TASK_COLUMNS = (
    Task.id,
    Task.project_id,
    Task.status,
    Task.context,
    Task.created_at,
    # compute_outer_loop_convergence -> _derive_completed_at reads the durable
    # terminal timestamp; a deferred read under raiseload=True raises, so it
    # must be eagerly loaded here.
    Task.completed_at,
    Task.latest_status_message,
)


async def _fetch_outer_loop_tasks(
    session: AsyncSession,
    *,
    cutoff: datetime,
    project_id: uuid.UUID | None,
    limit: int,
    order_desc: bool = False,
    lookback_cutoff: "datetime | None" = None,
) -> List[Task]:
    """Fetch the outer-loop window's terminal tasks with their payload type.

    The convergence summary walks every terminal non-review-chain task through
    ``task_outcomes.evaluate_task_outcome`` (the ``non_code_outcomes`` block),
    which resolves the task type from ``Task.first_payload``. Loading the full
    ``payloads`` JSONB array for a 5000-10000 row scan is the memory blow-up the
    digest's SQL-side projection exists to avoid, so on PostgreSQL we project
    just ``payloads[0]['type']`` and stash it for ``first_payload``; SQLite
    (tests/local) lacks usable JSON-path projection semantics and falls back to
    loading the column, mirroring ``admin_digest._compute_digest``.

    ``lookback_cutoff`` controls the ``created_at >=`` DB filter. When wider than
    ``cutoff`` (e.g. ``cutoff - hours`` to match the digest's 2x lookback), tasks
    created before the window but completing within it become visible to
    ``_compute_non_code_outcomes``, which windows on completion time not
    creation time. Defaults to ``cutoff`` (unchanged behaviour for callers that
    don't need the wider scan).
    """
    from app.sr_review_quality_metrics import (  # noqa: PLC0415
        _stash_projected_task_fields,
    )

    db_cutoff = lookback_cutoff if lookback_cutoff is not None else cutoff
    use_projection = resolve_dialect_name(session) == "postgresql"
    where_clauses = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= db_cutoff,
    ]
    if project_id is not None:
        where_clauses.append(Task.project_id == project_id)

    if use_projection:
        stmt = select(
            Task,
            Task.payloads[0]["type"].as_string().label("_first_payload_type"),
        ).options(load_only(*_OUTER_LOOP_TASK_COLUMNS, raiseload=True))
    else:
        stmt = select(Task).options(load_only(*_OUTER_LOOP_TASK_COLUMNS, Task.payloads, raiseload=True))
    stmt = stmt.where(*where_clauses)
    if order_desc:
        stmt = stmt.order_by(Task.created_at.desc())
    stmt = stmt.limit(limit)

    if not use_projection:
        return list((await session.execute(stmt)).scalars().all())

    tasks: List[Task] = []
    for row in (await session.execute(stmt)).all():
        task = row[0]
        # Don't clobber a richer projection stashed by an earlier query in the
        # same session: the digest's 5000-row scan stashes the FULL first
        # payload (plus a completed-at timestamp) on these same identity-mapped
        # instances before calling into this endpoint.
        if not hasattr(task, "_projected_first_payload"):
            _stash_projected_task_fields(
                task,
                first_payload={"type": row[1]} if row[1] else {},
            )
        tasks.append(task)
    return tasks


async def _hydrate_outcome_plan_outputs(
    session: AsyncSession, tasks: List[Task]
) -> None:
    """Batch-load ``plan_output`` for the rows whose outcome evaluator reads it.

    The read-only task types' honest success signals (audit findings, memo
    artifact, verify-tests verdict, compute result, panel verdict, knowledge
    document) live on ``plan_output``; without it ``evaluate_task_outcome``
    degrades them to ``neutral`` via ``_task_local_attr`` — silent
    under-counting in the digest's ``non_code_outcomes`` block. The window
    scans deliberately do NOT load that JSONB for every row, so hydrate just
    the rows the walker actually reads it on: terminal, outside a review chain,
    read-only typed, and non-research. Research outcomes use the finalized
    context signal and fail closed for legacy rows; loading their potentially
    100KB raw pre-gate output would be both wasteful and misleading. The
    re-SELECT populates the unloaded attribute on the already-tracked instances
    via the identity map.
    """
    from app.task_outcomes import _READ_ONLY_EVALUATORS  # noqa: PLC0415

    need: List[uuid.UUID] = []
    for t in tasks:
        if "plan_output" in vars(t):
            continue
        if t.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        ctx = t.context or {}
        if isinstance(ctx.get("review_loop"), dict):
            continue  # review-chain rows are excluded from the non-code walk
        t_type = (t.first_payload or {}).get("type")
        if t_type not in _READ_ONLY_EVALUATORS:
            continue
        if t_type == "research":
            continue
        need.append(t.id)
    if not need:
        return
    (
        await session.execute(
            select(Task)
            .options(load_only(Task.id, Task.plan_output, raiseload=True))
            .where(Task.id.in_(need))
        )
    ).all()


@router.get("/outer-loop-convergence")
async def get_outer_loop_convergence(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Aggregate OUTER review<->fix PR-cycle convergence health for a window.

    Sources the per-PR ``review_loop`` state and the early-escalation operator
    Alerts (does NOT change the convergence control logic). Returns the
    cycles-to-merge distribution, churn/drift totals, terminal-outcome counts,
    escalation-reason histogram, and the overall convergence rate. Suitable for
    the outer-loop convergence dashboard widget.
    """
    from app.digest_metrics import compute_outer_loop_convergence  # noqa: PLC0415

    cutoff = _utcnow() - timedelta(hours=hours)
    # Widen the DB scan by 1x hours so tasks created before the window but
    # completing within it are visible to _compute_non_code_outcomes (which
    # windows on completion time). Mirrors the 2x-lookback the digest uses.
    lookback_cutoff = cutoff - timedelta(hours=hours)

    tasks = await _fetch_outer_loop_tasks(
        session,
        cutoff=cutoff,
        project_id=project_id,
        limit=_OUTER_LOOP_TASK_CAP,
        order_desc=True,
        lookback_cutoff=lookback_cutoff,
    )
    if len(tasks) >= _OUTER_LOOP_TASK_CAP:
        logger.warning(
            "[OUTER-LOOP] task query hit the %d-row cap (hours=%d, project=%s); "
            "convergence aggregate may under-count PR-chains in this window",
            _OUTER_LOOP_TASK_CAP,
            hours,
            project_id,
        )
    original_tasks = await _load_outer_loop_original_tasks(
        session, tasks, project_id=project_id
    )
    await _hydrate_outcome_plan_outputs(session, [*tasks, *original_tasks.values()])
    alerts = await _load_escalation_alerts(
        session, cutoff=cutoff, project_id=project_id
    )

    summary = compute_outer_loop_convergence(
        all_tasks=[
            *tasks,
            *[
                task
                for oid, task in original_tasks.items()
                if oid not in {str(t.id) for t in tasks}
            ],
        ],
        escalation_alerts=alerts,
        window_start=cutoff,
    )
    return {"window_hours": hours, **summary}


@router.get("/outer-loop-convergence-trend")
async def get_outer_loop_convergence_trend(
    days: int = Query(7, ge=1, le=30),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-day buckets of outer review<->fix loop convergence over ``days``.

    Each bucket carries the per-day convergence rate, escalation total, and
    churn total so the widget can trend whether issue-churn is becoming rare and
    work is converging (merging) rather than oscillating.
    """
    from app.digest_metrics import (  # noqa: PLC0415
        compute_churn_direction,
        compute_outer_loop_convergence,
    )

    cutoff = _utcnow() - timedelta(days=days)

    tasks = await _fetch_outer_loop_tasks(
        session,
        cutoff=cutoff,
        project_id=project_id,
        limit=_OUTER_LOOP_TREND_TASK_CAP,
    )
    if len(tasks) >= _OUTER_LOOP_TREND_TASK_CAP:
        logger.warning(
            "[OUTER-LOOP] trend task query hit the %d-row cap (days=%d, project=%s); "
            "per-day convergence buckets may under-count PR-chains",
            _OUTER_LOOP_TREND_TASK_CAP,
            days,
            project_id,
        )
    original_tasks = await _load_outer_loop_original_tasks(
        session, tasks, project_id=project_id
    )
    await _hydrate_outcome_plan_outputs(session, [*tasks, *original_tasks.values()])
    alerts = await _load_escalation_alerts(
        session, cutoff=cutoff, project_id=project_id
    )

    # Bucket tasks and alerts by their creation day, then aggregate each day
    # through the same pure helper the point-in-time endpoint uses.
    tasks_by_day: Dict[str, List[Task]] = defaultdict(list)
    for t in tasks:
        if t.created_at is not None:
            tasks_by_day[t.created_at.strftime("%Y-%m-%d")].append(t)
    alerts_by_day: Dict[str, List[Alert]] = defaultdict(list)
    for a in alerts:
        if a.created_at is not None:
            alerts_by_day[a.created_at.strftime("%Y-%m-%d")].append(a)

    buckets = []
    for i in range(days - 1, -1, -1):
        day_dt = _utcnow() - timedelta(days=i)
        day = day_dt.strftime("%Y-%m-%d")
        day_start = day_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_tasks = list(tasks_by_day.get(day, []))
        day_task_ids = {str(task.id) for task in day_tasks}
        day_original_ids = set()
        for task in day_tasks:
            rl = (task.context or {}).get("review_loop")
            if isinstance(rl, dict):
                oid = rl.get("original_task_id")
                if isinstance(oid, str) and oid in original_tasks:
                    day_original_ids.add(oid)
        if day_original_ids:
            for oid in day_original_ids:
                original_task = original_tasks[oid]
                if str(original_task.id) not in day_task_ids:
                    day_tasks.append(original_task)
        summary = compute_outer_loop_convergence(
            all_tasks=day_tasks,
            escalation_alerts=alerts_by_day.get(day, []),
            window_start=day_start,
        )
        buckets.append(
            {
                "date": day,
                "pr_count": summary["prs_with_review_loop"],
                "convergence_rate": summary["convergence_rate"],
                "escalation_total": summary["escalation_total"],
                "churn_total": summary["churn_total"],
                "terminal_outcome_histogram": summary["terminal_outcome_histogram"],
                "incidence_counts": summary["incidence_counts"],
                "incidence_rates": summary["incidence_rates"],
            }
        )

    # Stamp the single canonical churn direction so the widget glyph and the
    # narrative verdict (digest_metrics.compute_narrative_summary) read the SAME
    # value rather than each re-deriving it client-/server-side.
    return {
        "days": days,
        "buckets": buckets,
        "churn_direction": compute_churn_direction(buckets),
    }


# Terminal-outcome / escalation reasons the chain drill-down can filter on. Kept
# in lockstep with the per-chain outcomes resolved by digest_metrics
# (_resolve_chain_outcome): the five non-merge blockers plus ``merged`` itself.
_OUTER_LOOP_DRILLDOWN_REASONS = (
    "rejected",
    "cap_exhausted",
    "stuck",
    "standoff",
    "repeat_fingerprint_halt",
    "merged",
)


@router.get("/outer-loop-chains")
async def get_outer_loop_chains(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    reason: str | None = Query(
        None,
        description=(
            "Filter chains by terminal outcome / escalation reason: rejected, "
            "cap_exhausted, stuck, standoff, repeat_fingerprint_halt, or merged. "
            "When omitted, all non-merged chains are returned."
        ),
    ),
    limit: int = Query(50, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """List the specific OUTER review<->fix PR-chains behind a terminal outcome.

    Outer-loop analog of ``/fix-loop-diverged-tasks``: where
    ``/outer-loop-convergence`` reports the dominant non-merge blocker as a
    single count, this lists the actual PR-chains so an operator can open and
    investigate them. Read-only; reuses the SAME ``require_admin`` dependency,
    row caps, original-task hydration, and chain grouping as the other
    outer-loop endpoints. When ``reason`` is omitted, all non-merged chains are
    returned (the actionable set); otherwise only chains matching that outcome.
    """
    from app.digest_metrics import _resolve_outer_loop_chains  # noqa: PLC0415

    if reason is not None and reason not in _OUTER_LOOP_DRILLDOWN_REASONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid reason '{reason}'. Must be one of: "
                f"{', '.join(_OUTER_LOOP_DRILLDOWN_REASONS)}."
            ),
        )

    cutoff = _utcnow() - timedelta(hours=hours)

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
                Task.context,
                Task.created_at,
            raiseload=True,)
        )
        .order_by(Task.created_at.desc())
        .limit(_OUTER_LOOP_TASK_CAP)
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)

    tasks = list((await session.execute(stmt)).scalars().all())
    if len(tasks) >= _OUTER_LOOP_TASK_CAP:
        logger.warning(
            "[OUTER-LOOP] chain drill-down task query hit the %d-row cap "
            "(hours=%d, project=%s); some PR-chains may be omitted",
            _OUTER_LOOP_TASK_CAP,
            hours,
            project_id,
        )
    original_tasks = await _load_outer_loop_original_tasks(
        session, tasks, project_id=project_id
    )
    alerts = await _load_escalation_alerts(
        session, cutoff=cutoff, project_id=project_id
    )

    chains = _resolve_outer_loop_chains(
        all_tasks=[
            *tasks,
            *[
                task
                for oid, task in original_tasks.items()
                if oid not in {str(t.id) for t in tasks}
            ],
        ],
        escalation_alerts=alerts,
        window_start=cutoff,
    )

    if reason is not None:
        selected = [c for c in chains if c["outcome"] == reason]
    else:
        selected = [
            c for c in chains if c["outcome"] is not None and c["outcome"] != "merged"
        ]

    rows = [
        {
            "pr_number": c["pr_number"],
            "task_ids": c["task_ids"],
            "max_iteration": c["max_iteration"],
            "churn_count": c["churn_count"],
            "terminal_reason": c["outcome"],
            "project_id": c["project_id"],
        }
        for c in selected[:limit]
    ]

    return {
        "window_hours": hours,
        "reason_filter": reason,
        "total": len(rows),
        "chains": rows,
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

    cutoff = _utcnow() - timedelta(hours=hours)

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
            raiseload=True,)
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

    doc = await read_project_sr_analytics(session, project)
    now = _utcnow()
    regressions: List[Dict[str, Any]] = []

    # SR catch-rate regression signals — read from the sr_catch_rate_trends_alert
    # design-doc flag (written by playbook-learning, not the removed dispatcher).
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

    # Fix-loop divergence regression signals — read from design-doc flags.
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

    viewport_cutoff = now - timedelta(days=_VIEWPORT_TASK_LOOKBACK_DAYS)
    viewport_task_rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.FAILED,
                    Task.created_at >= viewport_cutoff,
                )
                .options(
                    load_only(
                        Task.id,
                        Task.project_id,
                        Task.title,
                        Task.status,
                        Task.created_at,
                        Task.updated_at,
                        Task.latest_status_message,
                        Task.context,
                    raiseload=True,)
                )
                .order_by(Task.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    seen_viewport_classes: set[str] = set()
    for task in viewport_task_rows:
        is_viewport_failure, viewport_keys = _viewport_failure_context(task)
        if not is_viewport_failure:
            continue
        metadata = _viewport_metadata_for_failed_task(task, viewport_keys)
        if metadata:
            component = metadata["component"]
            assertion_type = metadata.get("assertion_type")
            width = metadata["width"]
            regression_class = (
                f"viewport_regression:{component}:{assertion_type}@{width}"
                if assertion_type
                else f"viewport_regression:{component}@{width}"
            )
            signal_name = f"Viewport regression: {component} at {width}px"
        else:
            component = None
            assertion_type = None
            width = None
            regression_class = f"viewport_regression:metadata_unavailable:{task.id}"
            signal_name = "Viewport regression: metadata unavailable"
        if regression_class in seen_viewport_classes:
            continue
        seen_viewport_classes.add(regression_class)
        regressions.append(
            {
                "signal_name": signal_name,
                "regression_class": regression_class,
                "detected_at": (task.updated_at or task.created_at).isoformat()
                if (task.updated_at or task.created_at)
                else None,
                "severity": "critical",
                "current_rate": None,
                "prior_rate": None,
                "resolved": False,
                "resolved_at": None,
                "details": task.latest_status_message,
                "viewport_component": component,
                "viewport_assertion_type": assertion_type,
                "viewport_width": width,
            }
        )

    # The detect->investigate->fix regression apparatus has been removed: these
    # signals are now surfaced purely for operator visibility (design-doc flags +
    # the Alert table). No autonomous remediation task is spawned, so the prior
    # ``spawned_task_*`` enrichment and the viewport-regression RegressionRemediationCase
    # listing are gone. The fields are kept (as None) so the admin-ui contract is stable.
    for r in regressions:
        r["spawned_task_id"] = None
        r["spawned_task_status"] = None
        r["spawned_task_title"] = None

    return {
        "project_id": str(project_id),
        "regressions": regressions,
        "healthy": len(regressions) == 0,
    }


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


@router.get("/projects/{project_id}/warm-reuse-dropout")
async def get_warm_reuse_dropout(
    project_id: uuid.UUID,
    window_hours: int = Query(default=24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Ranked warm-reuse drop-out diagnostic for the last ``window_hours``.

    Surfaces :func:`app.warm_backlog_reuse.compute_dominant_reuse_dropout`:
    a per-reason breakdown of why completed VMs with eligible backlog failed to
    convert to a warm claim, plus the single ``dominant_dropout`` (named only
    when one reason clearly dominates — see the gating in that helper). Mirrors
    the dominant-blocker pattern used for outer-loop convergence. Counters are
    in-memory and reset on instance restart; use the ``-trend`` endpoint for the
    restart-durable day-over-day view. This is observability only.
    """
    from app.warm_backlog_reuse import compute_dominant_reuse_dropout  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return compute_dominant_reuse_dropout(window_hours=window_hours)


@router.get("/projects/{project_id}/warm-reuse-dropout-trend")
async def get_warm_reuse_dropout_trend(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Day-over-day warm-reuse drop-out trend from persisted daily snapshots.

    Reads the ``reuse_dropout`` block persisted into each daily
    efficiency-snapshot (see :mod:`app.efficiency_snapshot`) so the dominant
    drop-out survives instance restarts and an operator can confirm whether a
    widening change reduced the leader. Returns an empty ``buckets`` list until
    at least one daily snapshot has captured the drop-out block.
    """
    from app.efficiency_snapshot import DESIGN_DOC_KEY, _read_snapshot_history  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    project = await session.get(Project, project_id)
    container = (getattr(project, "design_doc", None) or {}).get(DESIGN_DOC_KEY) or {}
    buckets: List[Dict[str, Any]] = []
    for snap in await _read_snapshot_history(session, project_id, container):
        block = snap.get("reuse_dropout")
        if not isinstance(block, dict):
            continue
        dominant = block.get("dominant_dropout") or {}
        buckets.append(
            {
                "captured_at": snap.get("captured_at"),
                "dropout_total": block.get("dropout_total"),
                "dominant_reason": dominant.get("reason"),
                "dominant_share": dominant.get("share"),
            }
        )
    return {"buckets": buckets}


@metrics_router.get("/digest-latency-stats")
async def get_digest_latency_stats(
    current_user: dict = Depends(get_metrics_reader),  # METRICS_READER_ENDPOINTS
) -> Dict[str, Any]:
    """Return in-process rolling latency aggregate for /api/admin/digest.

    Reports p50/p95 request durations (seconds) and per-tier serve counts
    since the last process restart. All values are per-instance — replicas
    maintain independent aggregates and this endpoint only reflects the
    instance that answered the request.
    """
    from app.digest_latency import get_digest_latency_snapshot  # noqa: PLC0415

    return get_digest_latency_snapshot()


@router.get("/projects/{project_id}/pool-dispatch-funnel-trend")
async def get_pool_dispatch_funnel_trend(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Day-over-day warm-POOL dispatch funnel trend from persisted snapshots.

    Reads the ``pool_dispatch_funnel`` block persisted into each daily
    efficiency-snapshot (see :mod:`app.efficiency_snapshot`) so the pool
    dispatch funnel — lease hits, cold dispatches, hit rate, the lease-miss
    breakdown, and the single dominant lease-miss reason — survives the
    instance restarts that reset the in-memory funnel counters. This is the
    restart-durable view of WHY the pool missed across the multi-day windows an
    operator watches during a controlled enable; a miss funnel dominated by
    ``no_compatible_member`` says enable account-retarget, one dominated by
    ``no_idle_member`` says grow the pool. A DIFFERENT funnel from
    ``warm-reuse-dropout-trend``. Returns an empty ``buckets`` list until at
    least one daily snapshot has captured the block. Observability only.
    """
    from app.efficiency_snapshot import DESIGN_DOC_KEY, _read_snapshot_history  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    project = await session.get(Project, project_id)
    container = (getattr(project, "design_doc", None) or {}).get(DESIGN_DOC_KEY) or {}
    buckets: List[Dict[str, Any]] = []
    for snap in await _read_snapshot_history(session, project_id, container):
        block = snap.get("pool_dispatch_funnel")
        if not isinstance(block, dict):
            continue
        dominant = block.get("dominant_miss") or {}
        buckets.append(
            {
                "captured_at": snap.get("captured_at"),
                "pool_lease_hits": block.get("pool_lease_hits"),
                "cold_dispatches": block.get("cold_dispatches"),
                "pool_hit_rate": block.get("pool_hit_rate"),
                "lease_miss_total": block.get("lease_miss_total"),
                "dominant_miss_reason": dominant.get("reason"),
                "dominant_miss_share": dominant.get("share"),
            }
        )
    return {"buckets": buckets}


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
                .options(load_only(Task.id, Task.status, Task.context, Task.created_at, raiseload=True))
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

    # Per-origin warm-start breakdown reuses the same helper that powers the
    # project-scoped efficiency-axes view, so the pool-vs-chain-vs-backlog split
    # shown next to ``warm_reuse_rate`` is computed identically (and over the
    # same per-window task bucket). Imported locally to avoid widening this
    # router's module-load import surface.
    from app.efficiency_snapshot import (  # noqa: PLC0415
        _compute_warm_start_origin_breakdown,
    )

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
            # Pool-vs-chain-vs-backlog split of the warm-start population over
            # this window, plus the pool-origin share. None when no warm starts
            # in the window (nothing to attribute). Lets an operator see whether
            # the pool — not chain-continuity/backlog-reuse — earned its idle cost.
            "warm_start_origin_breakdown": _compute_warm_start_origin_breakdown(bucket),
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


_SPAWN_GATE_CENSUS_TASK_CAP = 5000


@router.get("/spawn-gate-census")
async def get_spawn_gate_census(
    hours: int = Query(24, ge=1, le=168),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Project-wide windowed census of spawn-gate activity.

    Returns per-cycle gate invocations, grader attempt counts, parse-outcome
    and cause-category distributions, exhaustion and give-up totals. Covers
    undirected research cycles and deduplicates multi-direction cycles naturally
    (one row per research task.id).
    """
    from app.digest_direction_progress import compute_spawn_gate_census  # noqa: PLC0415
    from app.sr_review_quality_metrics import _stash_projected_task_fields  # noqa: PLC0415

    cutoff = _utcnow() - timedelta(hours=hours)

    where_clauses = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= cutoff,
    ]
    if project_id is not None:
        where_clauses.append(Task.project_id == project_id)

    use_projection = resolve_dialect_name(session) == "postgresql"
    if use_projection:
        stmt = (
            select(
                Task,
                Task.payloads[0]["type"].as_string().label("_first_payload_type"),
            )
            .where(*where_clauses)
            .options(load_only(Task.id, Task.project_id, Task.context, Task.payloads, raiseload=True))
            .limit(_SPAWN_GATE_CENSUS_TASK_CAP)
        )
    else:
        stmt = (
            select(Task)
            .where(*where_clauses)
            .options(load_only(Task.id, Task.project_id, Task.context, Task.payloads, raiseload=True))
            .limit(_SPAWN_GATE_CENSUS_TASK_CAP)
        )

    if use_projection:
        tasks: List[Task] = []
        for row in (await session.execute(stmt)).all():
            task = row[0]
            if not hasattr(task, "_projected_first_payload"):
                _stash_projected_task_fields(
                    task,
                    first_payload={"type": row[1]} if row[1] else {},
                )
            tasks.append(task)
    else:
        tasks = list((await session.execute(stmt)).scalars().all())

    if len(tasks) >= _SPAWN_GATE_CENSUS_TASK_CAP:
        logger.warning(
            "[SPAWN-GATE-CENSUS] query hit %d-row cap (hours=%d, project_id=%s); "
            "census may be incomplete",
            _SPAWN_GATE_CENSUS_TASK_CAP,
            hours,
            project_id,
        )

    census = compute_spawn_gate_census(tasks, window_start=cutoff)
    return {"window_hours": hours, **census}


# ---------------------------------------------------------------------------
# Admin-UI initial-load timing: persisted per surface
# ---------------------------------------------------------------------------

_UI_TIMING_LOCK = threading.Lock()
_UI_TIMING_SAMPLES: Dict[str, deque] = {}
_UI_TIMING_MAX_PER_SURFACE = 200


@router.post("/ui-load-timing")
async def record_ui_load_timing(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Record one admin-UI initial-load timing sample for a surface.

    Persisted to the database so samples survive process restarts and can
    be used for before/after deploy comparisons.  The in-memory ring buffer
    is retained for cross-test compatibility.
    """
    surface = str(payload.get("surface") or "unknown")[:64]
    sample_type = str(payload.get("sample_type") or "first_commit")[:32]
    ms_raw = payload.get("page_ready_ms")
    if not isinstance(ms_raw, (int, float)) or ms_raw < 0 or ms_raw > 600_000:
        raise HTTPException(status_code=400, detail="page_ready_ms must be a non-negative number ≤600000")
    ms = int(ms_raw)
    now = _utcnow()
    sample_row = UiLoadTimingSample(surface=surface, page_ready_ms=ms, sample_type=sample_type, recorded_at=now)
    session.add(sample_row)
    # Trim oldest rows beyond cap (keep newest _UI_TIMING_MAX_PER_SURFACE)
    cutoff_sq = (
        sa_select(UiLoadTimingSample.recorded_at)
        .where(UiLoadTimingSample.surface == surface)
        .order_by(UiLoadTimingSample.recorded_at.desc())
        .offset(_UI_TIMING_MAX_PER_SURFACE - 1)
        .limit(1)
        .scalar_subquery()
    )
    await session.execute(
        delete(UiLoadTimingSample).where(
            UiLoadTimingSample.surface == surface,
            UiLoadTimingSample.recorded_at < cutoff_sq,
        )
    )
    await session.commit()
    with _UI_TIMING_LOCK:
        q = _UI_TIMING_SAMPLES.setdefault(surface, deque(maxlen=_UI_TIMING_MAX_PER_SURFACE))
        q.append({"ts": now.isoformat(), "page_ready_ms": ms})
    return {"ok": True}


@router.post("/ui-load-timing/pin-baseline")
async def pin_ui_load_timing_baseline(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Compute and persist an immutable baseline from current samples.

    Reads all existing UiLoadTimingSample rows, computes p50/p90/count per
    (surface, sample_type), and stores the result as an immutable baseline
    that survives the 200-sample FIFO cap. The ``label`` field (max 128
    chars) is a human-readable tag, e.g. "Before lazy-load wave".
    """
    label = str(payload.get("label", "") or "")[:128]
    dialect_name = resolve_dialect_name(session)

    # rows is a list of (surface, sample_type, count, p50, p90).
    rows: list[tuple[str, str, int, int, int]] = []

    if dialect_name == "postgresql":
        # Grouped aggregate query with Postgres percentile_cont avoids loading
        # every UiLoadTimingSample row into Python memory (the previous unbounded
        # SELECT * performed a full table scan on every pin call).
        result = await session.execute(
            sa_select(
                UiLoadTimingSample.surface,
                UiLoadTimingSample.sample_type,
                func.count(UiLoadTimingSample.page_ready_ms).label("cnt"),
                func.percentile_cont(0.5).within_group(
                    UiLoadTimingSample.page_ready_ms.asc()
                ).label("p50"),
                func.percentile_cont(0.9).within_group(
                    UiLoadTimingSample.page_ready_ms.asc()
                ).label("p90"),
            ).group_by(
                UiLoadTimingSample.surface,
                UiLoadTimingSample.sample_type,
            )
        )
        for r in result.fetchall():
            rows.append((r.surface, r.sample_type, r.cnt, int(r.p50), int(r.p90)))
    else:
        # SQLite test-harness path: SQLite has no percentile_cont, so fetch all
        # rows and compute continuous percentiles in Python (test datasets are
        # small, so the full scan is acceptable).
        all_samples = (await session.execute(select(UiLoadTimingSample))).scalars().all()
        groups: dict = defaultdict(list)
        for s in all_samples:
            groups[(s.surface, s.sample_type)].append(s.page_ready_ms)
        for (surface, sample_type), vals in groups.items():
            vals.sort()
            n = len(vals)
            if n == 0:
                continue
            # Continuous percentile matching PostgreSQL percentile_cont.
            def _pcont(p: float) -> float:
                rn = p * (n - 1)
                lo = int(rn)
                hi = min(lo + 1, n - 1)
                frac = rn - lo
                return vals[lo] * (1 - frac) + vals[hi] * frac
            rows.append((surface, sample_type, n, int(_pcont(0.5)), int(_pcont(0.9))))

    baselines = []
    for surface, sample_type, cnt, p50, p90 in rows:
        bl = UiLoadTimingBaseline(
            surface=surface,
            sample_type=sample_type,
            p50=p50,
            p90=p90,
            count=cnt,
            label=label,
        )
        session.add(bl)
        baselines.append(bl)

    await session.commit()

    return {
        "ok": True,
        "label": label,
        "surface_count": len(baselines),
        "baselines": [
            {
                "id": str(b.id),
                "surface": b.surface,
                "sample_type": b.sample_type,
                "p50": b.p50,
                "p90": b.p90,
                "count": b.count,
                "label": b.label,
                "pinned_at": b.pinned_at.isoformat() if b.pinned_at else None,
            }
            for b in baselines
        ],
    }


@router.get("/ui-load-timing")
async def get_ui_load_timing(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Return recent admin-UI initial-load timing samples grouped by surface,
    plus any pinned baselines.

    Each surface entry is a list of {ts, page_ready_ms, sample_type} objects
    ordered oldest-first (up to 200 per surface).  The ``baselines`` array
    carries immutable baseline snapshots (p50/p90/count) that survive the
    per-surface FIFO cap.
    """
    rows = (
        await session.execute(
            select(UiLoadTimingSample)
            .order_by(UiLoadTimingSample.recorded_at.asc())
            .limit(5000)
        )
    ).scalars().all()
    surfaces: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        surfaces.setdefault(row.surface, []).append(
            {"ts": row.recorded_at.isoformat(), "page_ready_ms": row.page_ready_ms, "sample_type": row.sample_type}
        )

    baseline_rows = (
        await session.execute(
            select(UiLoadTimingBaseline)
            .order_by(UiLoadTimingBaseline.pinned_at.desc())
        )
    ).scalars().all()
    baselines = [
        {
            "id": str(b.id),
            "surface": b.surface,
            "sample_type": b.sample_type,
            "p50": b.p50,
            "p90": b.p90,
            "count": b.count,
            "label": b.label,
            "pinned_at": b.pinned_at.isoformat() if b.pinned_at else None,
        }
        for b in baseline_rows
    ]

    return {"surfaces": surfaces, "baselines": baselines}
