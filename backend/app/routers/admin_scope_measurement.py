"""Scope-level conversion rate measurement endpoints."""

import uuid
import logging
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.landed_signal import is_merged_completion
from app.models import Task, TaskStatus, Project, _utcnow
from app.auth import get_current_user, require_admin
from app.routers.admin_analytics import _derive_completed_at

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-scope-measurement"],
    dependencies=[Depends(get_current_user)],
)

# These endpoints aggregate over the FULL lookback window — every terminal task
# contributes to the computed baselines/rates, so a row LIMIT used as a page size
# would silently change the answer (and offset paging over an un-ordered aggregate
# skips/duplicates rows). Instead of paginating we apply a single deterministic
# safety cap that bounds worst-case memory without altering results in normal
# operation: realistic windows (≤730d at ~20-40 tasks/day ≈ 30k rows) stay well
# under the cap. Rows are ordered most-recent-first so that, in the pathological
# case where the cap is exceeded, the scan is deterministic across identical calls
# and the response advertises `truncated: true` rather than lying about a partial
# aggregate.
_MAX_AGGREGATION_SCAN = 50000


def _compute_scope_baselines(tasks: list, lookback_days: int) -> dict:
    """Compute per-scope merge rate baselines from a pre-fetched list of terminal tasks.

    Groups tasks by ISO-week (Monday-keyed) and scope (from context['scope']),
    computing merge_rate = COMPLETED / total terminal per bucket.  Also returns
    an overall summary per scope with trend derived from first-to-last week delta.
    """
    bucket: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"created": 0, "merged": 0}
    )

    for t in tasks:
        # `.get("scope", "unknown")` would return None if context has
        # {"scope": None} (explicit null). Mixing None with str in the
        # bucket keys makes `sorted(bucket.items())` raise TypeError on
        # Python 3 ("'<' not supported between NoneType and str"). The
        # `or` coerces both missing AND null to "unknown".
        scope = (t.context or {}).get("scope") or "unknown"
        task_date = t.created_at.date() if t.created_at else None
        if task_date is None:
            continue
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        bucket[(week_key, scope)]["created"] += 1
        if is_merged_completion(t):
            bucket[(week_key, scope)]["merged"] += 1

    # Build time-series sorted by week then scope
    series = []
    for (week, scope), counts in sorted(bucket.items()):
        created = counts["created"]
        merged = counts["merged"]
        merge_rate = round(merged / created, 3) if created > 0 else None
        series.append(
            {
                "week": week,
                "scope": scope,
                "created": created,
                "merged": merged,
                "merge_rate": merge_rate,
            }
        )

    # Build per-scope summary: overall rate + trend from first-to-last week
    all_scopes: set = {scope for (_, scope) in bucket}
    summary: Dict[str, Any] = {}
    for scope in sorted(all_scopes):
        total_created = sum(
            c["created"] for (_, sc), c in bucket.items() if sc == scope
        )
        total_merged = sum(c["merged"] for (_, sc), c in bucket.items() if sc == scope)
        overall_rate = (
            round(total_merged / total_created, 3) if total_created > 0 else None
        )

        scope_weeks = sorted((wk, c) for (wk, sc), c in bucket.items() if sc == scope)
        scope_trend: str | None = None
        if len(scope_weeks) >= 2:
            first_c = scope_weeks[0][1]
            last_c = scope_weeks[-1][1]
            first_rate = (
                first_c["merged"] / first_c["created"]
                if first_c["created"] > 0
                else None
            )
            last_rate = (
                last_c["merged"] / last_c["created"] if last_c["created"] > 0 else None
            )
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
    total_merged = sum(1 for t in medium_tasks if is_merged_completion(t))
    medium_scope_conversion_rate = (
        round(total_merged / total_created, 3) if total_created > 0 else None
    )

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

    def _window_stats(
        tasks_subset: list,
        window_start: Optional[datetime],
        window_end: Optional[datetime],
    ) -> Dict[str, Any]:
        """Count ``tasks_subset`` once, using terminal-updated-in-window
        semantics. Callers that pre-filter by created_at produce in-flight
        bias on short windows — use ``_rolling_window_stats`` for those.
        Kept for pre/post-investment comparisons where the window spans
        months and everything is long-terminal."""
        finished = [t for t in tasks_subset if t.status in _TERMINAL_STATUSES]
        created = len(finished)
        merged = sum(1 for t in finished if is_merged_completion(t))
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
            if is_merged_completion(t):
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
        pre_tasks = [
            t for t in medium_tasks if t.created_at and t.created_at < investment_start
        ]
        post_tasks = [
            t for t in medium_tasks if t.created_at and t.created_at >= investment_start
        ]

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
    week_bucket: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"created": 0, "merged": 0, "direction_merges": 0}
    )

    for t in medium_tasks:
        if not t.created_at:
            continue
        task_date = t.created_at.date()
        week_monday = task_date - timedelta(days=task_date.weekday())
        week_key = week_monday.isoformat()
        week_bucket[week_key]["created"] += 1
        is_merged = is_merged_completion(t)
        if is_merged:
            week_bucket[week_key]["merged"] += 1
        if (
            is_merged
            and resolved_direction_id
            and (t.context or {}).get("direction_id") == resolved_direction_id
        ):
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
            merge_rate_delta = (
                round(merge_rate - prev_rate, 3)
                if (merge_rate is not None and prev_rate is not None)
                else None
            )
        weekly_series.append(
            {
                "week": week_key,
                "created": created,
                "merged": merged,
                "merge_rate": merge_rate,
                "direction_merges": counts["direction_merges"],
                "merge_rate_delta": merge_rate_delta,
            }
        )

    week_rate_delta: Dict[str, Optional[float]] = {
        e["week"]: e["merge_rate_delta"] for e in weekly_series
    }

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
    day_bucket: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"created": 0, "merged": 0, "direction_merges": 0}
    )
    for t in medium_tasks:
        if t.status not in _TERMINAL_STATUSES:
            continue
        resolved_at = t.updated_at or t.created_at
        if not resolved_at or resolved_at < daily_cutoff:
            continue
        day_key = resolved_at.date().isoformat()
        day_bucket[day_key]["created"] += (
            1  # legacy key = denominator (finished-in-day)
        )
        is_merged = is_merged_completion(t)
        if is_merged:
            day_bucket[day_key]["merged"] += 1
        if (
            is_merged
            and resolved_direction_id
            and (t.context or {}).get("direction_id") == resolved_direction_id
        ):
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
            merge_rate_delta = (
                round(merge_rate - prev_rate, 3)
                if (merge_rate is not None and prev_rate is not None)
                else None
            )
        daily_series.append(
            {
                "day": day_key,
                "created": created,
                "merged": merged,
                "merge_rate": merge_rate,
                "direction_merges": counts["direction_merges"],
                "merge_rate_delta": merge_rate_delta,
            }
        )

    # --- Correlated changes: direction-associated merged tasks with weekly delta ---
    correlated_changes: List[Dict[str, Any]] = []
    if resolved_direction_id:
        for t in medium_tasks:
            if (t.context or {}).get("direction_id") != resolved_direction_id:
                continue
            if not is_merged_completion(t):
                continue
            if not t.created_at:
                continue
            task_date = t.created_at.date()
            week_monday = task_date - timedelta(days=task_date.weekday())
            week_key = week_monday.isoformat()
            correlated_changes.append(
                {
                    "title": t.title,
                    "week": week_key,
                    "merge_rate_delta": week_rate_delta.get(week_key),
                }
            )

    return {
        "medium_scope_conversion_rate": medium_scope_conversion_rate,
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
        measured = [
            t for t in task_list if "_first_attempt_succeeded" in (t.context or {})
        ]
        if not measured:
            return None
        successes = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(successes / len(measured), 2)

    measured_tasks = [
        t for t in tasks if "_first_attempt_succeeded" in (t.context or {})
    ]
    headline_rate = _fa_rate(tasks)
    sample_size = len(measured_tasks)

    # --- Pre/post split using split_date and _derive_completed_at ---
    pre_split: Optional[Dict[str, Any]] = None
    post_split: Optional[Dict[str, Any]] = None
    if split_date is not None:
        split_naive = (
            split_date.replace(tzinfo=None) if split_date.tzinfo else split_date
        )
        pre_tasks = [
            t
            for t in measured_tasks
            if _derive_completed_at(t).replace(tzinfo=None) < split_naive
        ]
        post_tasks = [
            t
            for t in measured_tasks
            if _derive_completed_at(t).replace(tzinfo=None) >= split_naive
        ]

        def _window_stats(
            task_subset: list,
            window_start: Optional[datetime],
            window_end: Optional[datetime],
        ) -> Dict[str, Any]:
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
    week_bucket: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"measured": 0, "succeeded": 0}
    )
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
            delta = (
                round(rate - prev_rate, 2)
                if (rate is not None and prev_rate is not None)
                else None
            )
        weekly_series.append(
            {
                "week": week_key,
                "rate": rate,
                "sample_size": measured,
                "delta_from_previous": delta,
            }
        )

    # --- By-category breakdown from _first_attempt_rejection_categories ---
    tasks_with_cats = [
        t for t in tasks if "_first_attempt_rejection_categories" in (t.context or {})
    ]
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
        Task.status.in_(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
        Task.created_at >= cutoff,
    ]
    if project_id:
        filters.append(Task.project_id == project_id)

    result = await session.execute(
        select(Task)
        .where(*filters)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.project_id,
                raiseload=True,
            )
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(_MAX_AGGREGATION_SCAN + 1)
    )
    tasks = list(result.scalars().all())
    truncated = len(tasks) > _MAX_AGGREGATION_SCAN
    if truncated:
        tasks = tasks[:_MAX_AGGREGATION_SCAN]

    data = _compute_scope_baselines(tasks, lookback_days)
    data["scanned"] = len(tasks)
    data["truncated"] = truncated
    return data


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
        select(Task)
        .where(
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            Task.created_at >= cutoff,
            Task.project_id == project_id,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.updated_at,
                Task.title,
                raiseload=True,
            )
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(_MAX_AGGREGATION_SCAN + 1)
    )
    all_tasks = list(result.scalars().all())
    truncated = len(all_tasks) > _MAX_AGGREGATION_SCAN
    if truncated:
        all_tasks = all_tasks[:_MAX_AGGREGATION_SCAN]
    medium_tasks = [t for t in all_tasks if (t.context or {}).get("scope") == "medium"]

    project = await session.get(Project, project_id)

    data = _compute_medium_scope_measurement(
        medium_tasks, project, direction_id, lookback_days
    )
    data["scanned"] = len(all_tasks)
    data["truncated"] = truncated
    return data


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
        select(Task)
        .where(
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            Task.created_at >= cutoff,
            Task.project_id == project_id,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.created_at,
                Task.project_id,
                Task.completed_at,
                raiseload=True,
            )
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(_MAX_AGGREGATION_SCAN + 1)
    )
    all_tasks = list(result.scalars().all())
    truncated = len(all_tasks) > _MAX_AGGREGATION_SCAN
    if truncated:
        all_tasks = all_tasks[:_MAX_AGGREGATION_SCAN]

    parsed_split_date: Optional[datetime] = None
    if split_date:
        try:
            parsed_split_date = datetime.fromisoformat(split_date)
        except (ValueError, TypeError):
            pass

    data = _compute_first_attempt_measurement(
        all_tasks, parsed_split_date, lookback_days
    )
    data["scanned"] = len(all_tasks)
    data["truncated"] = truncated
    return data
