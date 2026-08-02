"""Cycle-over-cycle trends for author self-review edge-case evidence."""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import StrategicCycle, _utcnow
from app.sr_catch_rate_trends import (
    _MIN_SR_SAMPLE,
    _SR_CODE_CHANGE_TYPES,
    _TREND_DELTA_THRESHOLD,
    _compute_edges_stats,
)

logger = logging.getLogger(__name__)


async def compute_edges_trend(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Compute a cycle-bucketed trend for author ``edges_considered`` evidence.

    Requires at least ``_MIN_SR_SAMPLE`` code-change tasks with an author
    self-review outcome in both the current and previous bucket. The result is
    persisted in ``ProjectSRAnalytics.data['edges_trend']``.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifying_count(tasks: list) -> int:
        return sum(
            1
            for task in tasks
            if (task.first_payload or {}).get("type") in _SR_CODE_CHANGE_TYPES
            and "_self_review_outcome" in (task.context or {})
        )

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for task in all_tasks:
            cycle_id = getattr(task, "strategic_cycle_id", None)
            if cycle_id:
                task_by_cycle.setdefault(cycle_id, []).append(task)
        for index in range(len(cycle_rows) - 1):
            current_cycle = cycle_rows[index]
            previous_cycle = cycle_rows[index + 1]
            current_tasks = task_by_cycle.get(current_cycle.id, [])
            previous_tasks = task_by_cycle.get(previous_cycle.id, [])
            if (
                _qualifying_count(current_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(previous_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = current_cycle.id
                previous_cycle_id = previous_cycle.id
                current_bucket = current_tasks
                previous_bucket = previous_tasks
                break

    if not use_cycle_mode:
        cutoff_14d = _naive(now - timedelta(days=14))
        cutoff_28d = _naive(now - timedelta(days=28))
        sentinel = datetime(9999, 12, 31)
        current_bucket = [
            task
            for task in all_tasks
            if task.created_at and cutoff_14d <= _naive(task.created_at) < sentinel
        ]
        previous_bucket = [
            task
            for task in all_tasks
            if task.created_at
            and cutoff_28d <= _naive(task.created_at) < cutoff_14d
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

    current_rate = current_stats["tasks_with_edges_rate"]
    previous_rate = previous_stats["tasks_with_edges_rate"]
    delta = round(current_rate - previous_rate, 3)
    if delta >= _TREND_DELTA_THRESHOLD:
        trend = "improving"
    elif delta <= -_TREND_DELTA_THRESHOLD:
        trend = "regressing"
    else:
        trend = "stable"

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": current_stats,
        "previous": previous_stats,
        "tasks_with_edges_rate_delta": delta,
        "trend": trend,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    def _store(data: dict) -> None:
        existing: dict = dict(data.get("edges_trend") or {})
        existing_computed_at: Optional[str] = existing.get("computed_at")

        is_new_window = True
        if use_cycle_mode:
            is_new_window = existing.get("current_cycle_id") != current_cycle_id
        elif existing_computed_at:
            try:
                last_dt = datetime.fromisoformat(existing_computed_at)
                is_new_window = (now - last_dt).days >= 14
            except (ValueError, TypeError):
                pass

        rate_history: list = list(existing.get("rate_history") or [])
        if is_new_window or not rate_history:
            rate_history.append(current_rate)
            rate_history = rate_history[-5:]
        result["rate_history"] = rate_history
        data["edges_trend"] = result

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
