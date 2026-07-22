"""Fresh-reviewer and edges-considered cycle-over-cycle trends.

Extracted from ``sr_analytics.py``. ``sr_analytics`` re-exports all public
names from this module for backward compatibility.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import StrategicCycle, TaskStatus, _utcnow
from app.sr_blind_spots import (
    _REVIEWER_ORIGINS,
    reviewer_confirmed_categories,
    safe_check_results,
)
from app.sr_catch_rate_trends import (
    _MIN_SR_SAMPLE,
    _SR_CODE_CHANGE_TYPES,
    _TREND_DELTA_THRESHOLD,
    _compute_edges_stats,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fresh-reviewer cycle-over-cycle trend
# ---------------------------------------------------------------------------

_FR_CODE_CHANGE_TYPES = {"create", "fix", "refactor", "migrate"}


async def compute_fresh_reviewer_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for fresh-reviewer effectiveness.

    Mirrors the cycle-vs-calendar bucketing from ``compute_sr_catch_rate_trends``.
    Requires at least ``_MIN_SR_SAMPLE`` tasks *with* reviewer_findings in both
    the current and previous bucket before computing rates.

    Output keys per category: ``per_category_hit_rate``, ``added_value``,
    ``approval_rate_lift`` — each with ``current``, ``previous``, ``delta``,
    and ``trend`` (stable / improving / regressing).

    **Side effect**: writes result to
    ``ProjectSRAnalytics.data["fresh_reviewer_trend"]``
    via :func:`~app.task_locking.with_sr_analytics_locked`.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    _code_tasks = [
        t
        for t in all_tasks
        if (t.first_payload or {}).get("type") in _FR_CODE_CHANGE_TYPES
    ]

    def _bucket_stats(tasks: list) -> dict[str, Any]:
        """Compute fresh-reviewer stats for a pre-filtered task list."""
        with_reviewer = [
            t
            for t in tasks
            if (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]
        without_reviewer = [
            t
            for t in tasks
            if not (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_findings")
        ]

        def _fa_rate(ts: list) -> Optional[float]:
            measured = [
                t for t in ts if "_first_attempt_succeeded" in (t.context or {})
            ]
            if not measured:
                return None
            return round(
                sum(1 for t in measured if t.context["_first_attempt_succeeded"])
                / len(measured),
                3,
            )

        cat_flagged: dict[str, int] = {}
        cat_confirmed: dict[str, int] = {}
        cat_added_value: dict[str, int] = {}
        for t in with_reviewer:
            ctx = t.context or {}
            findings = (
                ctx.get("_self_review_outcome", {}).get("reviewer_findings") or []
            )
            reviewer_confirmed_cats = reviewer_confirmed_categories(ctx)
            check_results = safe_check_results(ctx.get("_self_review_outcome", {}))
            # Author SR failures only — a reviewer-stamped override means the
            # author SR did NOT catch the category, so it must still count
            # toward `cat_added_value` when the human confirms the rescue.
            agent_failed = {
                c
                for c, r in check_results.items()
                if isinstance(r, dict)
                and not r.get("passed", True)
                and r.get("origin") not in _REVIEWER_ORIGINS
            }
            for finding in findings:
                cat = finding.get("category") if isinstance(finding, dict) else None
                if not cat:
                    continue
                cat_flagged[cat] = cat_flagged.get(cat, 0) + 1
                if cat in reviewer_confirmed_cats:
                    cat_confirmed[cat] = cat_confirmed.get(cat, 0) + 1
                    if cat not in agent_failed:
                        cat_added_value[cat] = cat_added_value.get(cat, 0) + 1

        return {
            "sample_size": len(with_reviewer),
            "cat_flagged": cat_flagged,
            "cat_confirmed": cat_confirmed,
            "cat_added_value": cat_added_value,
            "fa_rate_with": _fa_rate(with_reviewer),
            "fa_rate_without": _fa_rate(without_reviewer),
        }

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_stats: dict[str, Any] = {}
    previous_stats: dict[str, Any] = {}

    cycle_stmt = (
        # id-only projection (bucketing below uses only .id) + the
        # ix_strategiccycle_project_outcome_phase1 index: ordered index scan
        # satisfies LIMIT 4 directly (was a full scan + sort of every completed
        # cycle → 120s statement_timeout that poisoned the claim, 2026-05-29).
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in _code_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_stats = _bucket_stats(task_by_cycle.get(c_cycle.id, []))
            p_stats = _bucket_stats(task_by_cycle.get(p_cycle.id, []))
            if (
                c_stats["sample_size"] >= _MIN_SR_SAMPLE
                and p_stats["sample_size"] >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_stats = c_stats
                previous_stats = p_stats
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        cal_current = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        cal_previous = [
            t
            for t in _code_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]
        current_stats = _bucket_stats(cal_current)
        previous_stats = _bucket_stats(cal_previous)

    if (
        current_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
        or previous_stats.get("sample_size", 0) < _MIN_SR_SAMPLE
    ):
        return None

    def _classify(curr: Optional[float], prev: Optional[float]) -> str:
        if curr is None or prev is None:
            return "stable"
        delta = curr - prev
        if delta >= _TREND_DELTA_THRESHOLD:
            return "improving"
        if delta <= -_TREND_DELTA_THRESHOLD:
            return "regressing"
        return "stable"

    # Per-category hit rate
    c_flagged = current_stats["cat_flagged"]
    p_flagged = previous_stats["cat_flagged"]
    all_cats = set(c_flagged.keys()) | set(p_flagged.keys())

    per_category_hit_rate: dict[str, Any] = {}
    for cat in all_cats:
        c_total = c_flagged.get(cat, 0)
        p_total = p_flagged.get(cat, 0)
        c_rate = (
            round(current_stats["cat_confirmed"].get(cat, 0) / c_total, 3)
            if c_total >= 2
            else None
        )
        p_rate = (
            round(previous_stats["cat_confirmed"].get(cat, 0) / p_total, 3)
            if p_total >= 2
            else None
        )
        per_category_hit_rate[cat] = {
            "current": c_rate,
            "previous": p_rate,
            "delta": round(c_rate - p_rate, 3)
            if c_rate is not None and p_rate is not None
            else None,
            "trend": _classify(c_rate, p_rate),
        }

    # Added value (fresh-reviewer catches what agent self-review missed)
    c_av = current_stats["cat_added_value"]
    p_av = previous_stats["cat_added_value"]
    av_cats = set(c_av.keys()) | set(p_av.keys())
    added_value: dict[str, Any] = {}
    for cat in av_cats:
        c_cnt = c_av.get(cat, 0)
        p_cnt = p_av.get(cat, 0)
        added_value[cat] = {
            "current": c_cnt,
            "previous": p_cnt,
            "trend": _classify(float(c_cnt), float(p_cnt)),
        }

    # Approval rate lift
    c_with = current_stats["fa_rate_with"]
    c_without = current_stats["fa_rate_without"]
    p_with = previous_stats["fa_rate_with"]
    p_without = previous_stats["fa_rate_without"]
    c_lift = (
        round(c_with - c_without, 3)
        if c_with is not None and c_without is not None
        else None
    )
    p_lift = (
        round(p_with - p_without, 3)
        if p_with is not None and p_without is not None
        else None
    )
    approval_rate_lift: dict[str, Any] = {
        "current": c_lift,
        "previous": p_lift,
        "delta": round(c_lift - p_lift, 3)
        if c_lift is not None and p_lift is not None
        else None,
        "trend": _classify(c_lift, p_lift),
    }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current_sample_size": current_stats["sample_size"],
        "previous_sample_size": previous_stats["sample_size"],
        "per_category_hit_rate": per_category_hit_rate,
        "added_value": added_value,
        "approval_rate_lift": approval_rate_lift,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    _result = result

    def _store(data: dict) -> None:
        data["fresh_reviewer_trend"] = _result

    applied = await with_sr_analytics_locked(session, project_id, _store)
    if not applied:
        return None

    logger.info(
        "[FRESH_REVIEWER_TREND] project=%s categories=%d bucket_mode=%s",
        project_id,
        len(per_category_hit_rate),
        result["bucket_mode"],
    )

    return result


async def compute_sr_reviewer_disagreements(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
    *,
    window_days: int = 30,
    limit: int = 20,
) -> Optional[dict[str, Any]]:
    """Aggregate categories where SR passed but the fresh reviewer flagged an issue.

    Scans ``all_tasks`` (already fetched by the caller) for completed
    code-change tasks that have both ``check_results`` and
    ``reviewer_findings`` in their ``_self_review_outcome``. A disagreement
    is counted for every (category, task_type) pair where the fresh reviewer
    raised a finding but self-review either omitted the category or marked it
    ``passed: true``.

    **Side effect**: writes the result to
    ``ProjectSRAnalytics.data["sr_reviewer_disagreements"]``
    via :func:`~app.task_locking.with_sr_analytics_locked` so downstream readers
    (admin digest, agent project-context) can access pre-computed data without
    re-scanning tasks.

    Returns the result dict, or ``None`` when no disagreements are found.
    """
    from app.sr_blind_spots import _count_reviewer_disagreement_directions  # noqa: PLC0415

    now = _utcnow()
    cutoff = now - timedelta(days=window_days)

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    cutoff_naive = _naive(cutoff)

    eligible = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and (not t.created_at or _naive(t.created_at) >= cutoff_naive)
    ]
    direction_counts = _count_reviewer_disagreement_directions(eligible)

    if not any(bucket["miss"] > 0 for bucket in direction_counts.values()):
        return None

    top = sorted(
        [(k, v) for k, v in direction_counts.items() if v["miss"] > 0],
        key=lambda x: -x[1]["miss"],
    )[:limit]
    entries = [
        {
            "category": cat,
            "task_type": ttype,
            "count": bucket["miss"],
            "examples": bucket["examples"],
        }
        for (cat, ttype), bucket in top
    ]
    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "window_days": window_days,
        "disagreements": entries,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    _result = result

    def _store(data: dict) -> None:
        data["sr_reviewer_disagreements"] = _result

    await with_sr_analytics_locked(session, project_id, _store)

    logger.info(
        "[SR_REVIEWER_DISAGREEMENTS] project=%s entries=%d",
        project_id,
        len(entries),
    )
    return result


# ---------------------------------------------------------------------------
# Edges-considered cycle-over-cycle trend
# ---------------------------------------------------------------------------


async def compute_edges_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cycle-bucketed trend for edges_considered metrics.

    Mirrors the cycle-vs-calendar bucketing from ``compute_fresh_reviewer_trend``.
    Requires at least ``_MIN_SR_SAMPLE`` code-change tasks with
    ``_self_review_outcome`` in both the current and previous bucket.

    Primary trend signal: ``tasks_with_edges_rate`` — the fraction of code tasks
    that included an ``edges_considered`` list in their self-review.  Trend is
    classified as ``'improving'`` | ``'regressing'`` | ``'stable'`` relative to the
    previous bucket using ``_TREND_DELTA_THRESHOLD``.

    **Side effect**: writes result to ``ProjectSRAnalytics.data['edges_trend']``
    via :func:`~app.task_locking.with_sr_analytics_locked` and maintains a
    top-level ``rate_history`` list (capped at 5 entries).  Append rule mirrors
    ``sr_catch_rate_trends``: in cycle mode append on new ``current_cycle_id``;
    in calendar mode append when ≥14 days since last
    ``computed_at``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifying_count(tasks: list) -> int:
        return sum(
            1
            for t in tasks
            if (t.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
            and "_self_review_outcome" in (t.context or {})
        )

    # --- Cycle-based bucketing (primary mode) ---
    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        # id-only projection (bucketing below uses only .id) + the
        # ix_strategiccycle_project_outcome_phase1 index: ordered index scan
        # satisfies LIMIT 4 directly (was a full scan + sort of every completed
        # cycle → 120s statement_timeout that poisoned the claim, 2026-05-29).
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if (
        _qualifying_count(current_bucket) < _MIN_SR_SAMPLE
        or _qualifying_count(previous_bucket) < _MIN_SR_SAMPLE
    ):
        return None

    current_stats = _compute_edges_stats(current_bucket)
    previous_stats = _compute_edges_stats(previous_bucket)

    if current_stats is None or previous_stats is None:
        return None

    c_rate = current_stats["tasks_with_edges_rate"]
    p_rate = previous_stats["tasks_with_edges_rate"]
    delta = round(c_rate - p_rate, 3)
    if delta >= _TREND_DELTA_THRESHOLD:
        trend = "improving"
    elif delta <= -_TREND_DELTA_THRESHOLD:
        trend = "regressing"
    else:
        trend = "stable"

    rc_current = current_stats.get("reviewer_caught_rate")
    rc_previous = previous_stats.get("reviewer_caught_rate")
    if rc_current is not None and rc_previous is not None:
        rc_delta = round(rc_current - rc_previous, 3)
        if rc_delta >= _TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "improving"
        elif rc_delta <= -_TREND_DELTA_THRESHOLD:
            reviewer_caught_trend = "regressing"
        else:
            reviewer_caught_trend = "stable"
    else:
        rc_delta = None
        reviewer_caught_trend = None

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": current_stats,
        "previous": previous_stats,
        "tasks_with_edges_rate_delta": delta,
        "trend": trend,
        "reviewer_caught_rate_delta": rc_delta,
        "reviewer_caught_trend": reviewer_caught_trend,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    _now = now
    _use_cycle_mode = use_cycle_mode
    _current_cycle_id = current_cycle_id
    _c_rate = c_rate
    _result = result

    def _store(data: dict) -> None:
        existing: dict = dict(data.get("edges_trend") or {})
        existing_computed_at: Optional[str] = existing.get("computed_at")

        is_new_window = True
        if _use_cycle_mode:
            is_new_window = existing.get("current_cycle_id") != _current_cycle_id
        elif existing_computed_at:
            try:
                last_dt = datetime.fromisoformat(existing_computed_at)
                is_new_window = (_now - last_dt).days >= 14
            except (ValueError, TypeError):
                pass

        rate_history: list = list(existing.get("rate_history") or [])
        if is_new_window or not rate_history:
            rate_history.append(_c_rate)
            rate_history = rate_history[-5:]
        _result["rate_history"] = rate_history
        data["edges_trend"] = _result

    applied = await with_sr_analytics_locked(session, project_id, _store)
    if not applied:
        return None

    logger.info(
        "[EDGES_TREND] project=%s trend=%s bucket_mode=%s",
        project_id,
        trend,
        result["bucket_mode"],
    )

    return result

