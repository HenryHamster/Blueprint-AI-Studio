"""Global max-plan slot-fleet utilization — a real-time observability read.

How much of the shared account pool's concurrent-task capacity is in use right
now and on a short trailing average. This lives apart from ``max_plan_pool``
(which owns routing/selection and is already a large module) because it is a
read-only signal consumed by the strategic loop, not part of dispatch.

The instantaneous active count is read from the DB (tasks PROVISIONING/
IN_PROGRESS), **not** the in-memory ``active_tasks`` counter — that counter can
leak (a missed ``release_max_plan_account`` on a dispatch early-exit) and would
read artificially high, masking the very idle headroom this signal exists to
surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func, or_
from sqlmodel import select

from app import max_plan_pool
from app.models import Task, TaskStatus, _utcnow


def _sum_slot_occupancy_seconds(
    rows: list, now: datetime, window_start: datetime
) -> float:
    """Sum each task's slot-occupancy seconds within ``[window_start, now]``.

    Pure helper (no DB) so the time-weighted utilization average is unit-
    testable. ``rows`` are ``(status, created_at, updated_at, vm_dispatched_at,
    vm_provisioned_at, vm_stopped_at)`` tuples. A task occupies an account slot
    from when it was dispatched/provisioned (or, for a still-active task with
    neither stamp yet, its ``created_at``) until it stopped (or, for a still-
    active task, ``now``). Terminal tasks that never recorded a VM stamp never
    held a slot and contribute zero.
    """
    active_states = (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)

    def _naive(dt: datetime | None) -> datetime | None:
        return (
            dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt
        )

    total = 0.0
    for status, created_at, updated_at, dispatched, provisioned, stopped in rows:
        is_active = status in active_states
        start = dispatched or provisioned or (created_at if is_active else None)
        if start is None:
            continue
        end = stopped or (now if is_active else updated_at) or now
        start = _naive(start)
        end = _naive(end)
        if start is None or end is None:
            continue
        lo = max(start, window_start)
        hi = min(end, now)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


async def get_fleet_utilization_snapshot(
    session=None,
    *,
    window_hours: int = 24,
) -> Dict[str, Any]:
    """Global max-plan slot utilization — instantaneous + short trailing average.

    Capacity is the summed per-account ``max_concurrent_tasks`` of enabled
    accounts. ``util_avg`` is a time-weighted average concurrency over the
    trailing ``window_hours``: total task slot-occupancy seconds in the window
    divided by ``capacity * window_seconds`` — "what fraction of slots were busy
    over the last day", the chronic-headroom signal. ``usable_capacity``
    excludes accounts currently in cooldown so a low utilization caused by
    rate-limits (not idle headroom) is distinguishable.

    ``session`` is optional: with ``None`` the DB-derived fields are 0/None and
    only the in-memory capacity aggregation is returned (used by unit tests).
    """
    nominal_capacity = 0
    usable_capacity = 0
    cooled_accounts = 0
    for acct in max_plan_pool._all_accounts():
        if not acct.get("enabled", True):
            continue
        cap = acct.get("max_concurrent_tasks")
        if not isinstance(cap, int) or cap <= 0:
            continue
        nominal_capacity += cap
        if max_plan_pool._account_available_for_model(
            acct, max_plan_pool._OPUS_MODEL
        ):
            usable_capacity += cap
        else:
            cooled_accounts += 1

    now = _utcnow()
    window_seconds = max(1, window_hours * 3600)
    window_start = now - timedelta(hours=window_hours)

    active_now = 0
    util_now: float | None = None
    util_avg: float | None = None
    if session is not None:
        active_now = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        )
                    )
                )
            ).scalar()
            or 0
        )
        rows = (
            await session.execute(
                select(
                    Task.status,
                    Task.created_at,
                    Task.updated_at,
                    Task.vm_dispatched_at,
                    Task.vm_provisioned_at,
                    Task.vm_stopped_at,
                )
                .where(
                    or_(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        ),
                        Task.updated_at >= window_start,
                    )
                )
                .limit(5000)
            )
        ).all()
        occupancy_seconds = _sum_slot_occupancy_seconds(rows, now, window_start)
        if nominal_capacity:
            util_now = active_now / nominal_capacity
            util_avg = occupancy_seconds / window_seconds / nominal_capacity

    return {
        "active_now": active_now,
        "nominal_capacity": nominal_capacity or None,
        "usable_capacity": usable_capacity or None,
        "cooled_accounts": cooled_accounts,
        "util_now": round(util_now, 4) if util_now is not None else None,
        "util_avg": round(util_avg, 4) if util_avg is not None else None,
        "window_hours": window_hours,
    }
