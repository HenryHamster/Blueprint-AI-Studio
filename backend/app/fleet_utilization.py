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

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func, or_
from sqlmodel import select

from app import max_plan_pool
from app.models import FleetSizingReading, LocalRunner, Task, TaskStatus, _utcnow
from app.runtime_config_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)


def _naive(dt: datetime | None) -> datetime | None:
    """Strip tzinfo when present (all app columns are naive-UTC)."""
    return (
        dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt
    )


def _task_occupancy_interval(
    status,
    created_at,
    updated_at,
    dispatched,
    provisioned,
    stopped,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Half-open slot-occupancy interval ``[start, end)`` for one task row.

    A task occupies an account slot from when it was dispatched/provisioned
    (or, for a still-active task with neither stamp yet, its ``created_at``)
    until it stopped (or, for a still-active task, ``now``). Terminal tasks
    that never recorded a VM stamp never held a slot and return None. This is
    the single source of truth for the start/end pair shared by the
    time-weighted occupancy average and the concurrency-overlap measurement,
    so the two signals cannot disagree about what counts as occupying a slot.
    """
    active_states = (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)
    is_active = status in active_states
    start = dispatched or provisioned or (created_at if is_active else None)
    if start is None:
        return None
    end = stopped or (now if is_active else updated_at) or now
    start = _naive(start)
    end = _naive(end)
    if start is None or end is None:
        return None
    return start, end


def _sum_slot_occupancy_seconds(
    rows: list, now: datetime, window_start: datetime
) -> float:
    """Sum each task's slot-occupancy seconds within ``[window_start, now]``.

    Pure helper (no DB) so the time-weighted utilization average is unit-
    testable. ``rows`` are ``(status, created_at, updated_at, vm_dispatched_at,
    vm_provisioned_at, vm_stopped_at)`` tuples. Occupancy intervals are derived
    by ``_task_occupancy_interval`` (see its docstring for the slot
    definition); each interval is clipped to ``[window_start, now]``.
    """
    total = 0.0
    for row in rows:
        interval = _task_occupancy_interval(*row, now)
        if interval is None:
            continue
        start, end = interval
        lo = max(start, window_start)
        hi = min(end, now)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


# The slot definition used by every concurrency-overlap measurement. Kept in
# one place so the reported number always names the timestamp pair it is
# derived from.
SLOT_DEFINITION = (
    "vm_dispatched_at/vm_provisioned_at -> vm_stopped_at (still-active tasks "
    "extend to now); terminal tasks that never recorded a VM stamp hold no slot"
)


def _percentiles_from_histogram(
    hist: Dict[int, float], total_seconds: float, peak: int
) -> Dict[str, int]:
    """Time-weighted concurrency percentiles from a ``{level: seconds}`` histogram.

    ``pN`` is the concurrency level at or below which the fleet spent at least
    N% of the measured window, so ``p50`` is the median instantaneous
    concurrency and ``p100`` is the peak. The histogram spans from the first
    task's start to the last task's end, so interior idle gaps count as level 0.
    """
    if total_seconds <= 0:
        return {}
    labels = [("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99), ("p100", 100)]
    cum = 0.0
    result: Dict[str, int] = {}
    idx = 0
    for level in sorted(hist):
        cum += hist[level]
        while idx < len(labels) and cum >= total_seconds * labels[idx][1] / 100.0:
            result[labels[idx][0]] = level
            idx += 1
    result.setdefault("p100", peak)
    return result


def compute_concurrency_overlap(intervals) -> Dict[str, Any]:
    """Peak + time-weighted distribution of concurrent half-open intervals.

    Standard interval-overlap sweep: ``+1`` at each start, ``-1`` at each end,
    running maximum — a measurement, not a sampling or estimate. Intervals are
    half-open ``[start, end)``, so exactly-abutting intervals (``end == start``)
    do NOT overlap. Events sharing a timestamp are applied ends-first so a task
    ending at ``t`` and one starting at ``t`` are never counted together. The
    returned histogram counts seconds at each concurrency level over the active
    window (first start .. last end), including interior idle (level 0) gaps.
    """
    clean = [
        (s, e)
        for s, e in intervals
        if s is not None and e is not None and s < e
    ]
    if not clean:
        return {
            "peak": 0,
            "task_count": 0,
            "window_seconds": 0.0,
            "concurrency_histogram": {},
            "percentiles": {},
        }

    events = []
    for start, end in clean:
        events.append((start, 1))
        events.append((end, -1))
    # Sort by (timestamp, delta): an end (-1) sorts before a start (+1) at the
    # same timestamp, which is what makes the sweep half-open.
    events.sort(key=lambda e: (e[0], e[1]))

    active = 0
    peak = 0
    prev_ts = events[0][0]
    histogram: Dict[int, float] = {}
    total_seconds = 0.0
    for ts, delta in events:
        if ts > prev_ts:
            secs = (ts - prev_ts).total_seconds()
            if secs > 0:
                histogram[active] = histogram.get(active, 0.0) + secs
                total_seconds += secs
                if active > peak:
                    peak = active
        active += delta
        prev_ts = ts

    return {
        "peak": peak,
        "task_count": len(clean),
        "window_seconds": round(total_seconds, 3),
        "concurrency_histogram": {str(k): round(v, 3) for k, v in histogram.items()},
        "percentiles": _percentiles_from_histogram(histogram, total_seconds, peak),
    }


def _overlap_stats_from_task_rows(rows, now: datetime, window_start: datetime) -> Dict[str, Any]:
    """Compute concurrency-overlap stats from raw task rows.

    ``rows`` is an iterable of ``(status, created_at, updated_at,
    vm_dispatched_at, vm_provisioned_at, vm_stopped_at)`` tuples — the same
    shape ``_sum_slot_occupancy_seconds`` consumes. Each task's occupancy
    interval is derived by ``_task_occupancy_interval`` and clipped to
    ``[window_start, now]`` before the overlap sweep.
    """
    intervals = []
    for row in rows:
        interval = _task_occupancy_interval(*row, now)
        if interval is None:
            continue
        start, end = interval
        lo = max(start, window_start)
        hi = min(end, now)
        if hi > lo:
            intervals.append((lo, hi))
    return compute_concurrency_overlap(intervals)


async def get_task_concurrency_snapshot(
    session,
    *,
    window_hours: int = 24,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Measure peak/percentile concurrent slot-occupancy from the task table.

    Unlike ``get_fleet_utilization_snapshot`` (which reports instantaneous
    ``util_now`` and the time-weighted ``util_avg``), this derives the actual
    *distribution* of simultaneous slot-occupying tasks by sweeping each task's
    lifecycle interval across the window — the fleet-sizing evidence the mocked
    scheduler simulation previously stood in for.

    ``now`` defaults to ``_utcnow()``; pass an explicit value for deterministic
    tests. With ``session=None`` all measured fields are zero/empty.
    """
    now = _utcnow() if now is None else _naive(now)
    window_hours = max(1, int(window_hours))
    window_start = now - timedelta(hours=window_hours)

    if session is None:
        return {
            "slot_definition": SLOT_DEFINITION,
            "window_start": window_start.isoformat(),
            "window_end": now.isoformat(),
            "window_hours": window_hours,
            "peak": 0,
            "task_count": 0,
            "window_seconds": 0.0,
            "concurrency_histogram": {},
            "percentiles": {},
        }

    rows = (
        await session.execute(
            select(
                Task.status,
                Task.created_at,
                Task.updated_at,
                Task.vm_dispatched_at,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
            ).where(
                or_(
                    Task.status.in_(
                        [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                    ),
                    Task.updated_at >= window_start,
                )
            )
        )
    ).all()

    stats = _overlap_stats_from_task_rows(rows, now, window_start)
    return {
        "slot_definition": SLOT_DEFINITION,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "window_hours": window_hours,
        **stats,
    }


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

    ``concurrent_peak`` / ``concurrent_p50`` / ``concurrent_p90`` /
    ``concurrent_p95`` / ``concurrent_p99`` (populated only when ``session`` is
    given) add the measured task-slot concurrency distribution, computed from
    the same bounded task rows by ``_overlap_stats_from_task_rows`` — the one
    sweep shared with ``get_task_concurrency_snapshot``, so the two surfaces can
    never disagree. ``concurrent_peak`` is the maximum number of simultaneously
    occupied slots observed in the window. The percentiles use that sweep's
    denominator: seconds are weighted over the **active window** (first slot
    start .. last slot end, interior idle gaps counted at level 0), *not* over
    the full ``window_hours`` — leading/trailing idle does not drag the
    distribution toward 0. They are integer concurrency levels.

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
    concurrent_peak: int | None = None
    concurrent_p50: int | None = None
    concurrent_p90: int | None = None
    concurrent_p95: int | None = None
    concurrent_p99: int | None = None
    active_task_vms: int | None = None
    managed_vm_count: int | None = None
    idle_pool_vms: int | None = None
    orphaned_leased_vms: int | None = None
    vm_slots_available: int | None = None
    vm_saturation: str | None = None

    vm_cap: int = int(_runtime_config.get("max_concurrent_vms") or 0)

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

        # Same rows, same slot definition, same sweep as
        # get_task_concurrency_snapshot — measured overlap, not a simulation.
        overlap = _overlap_stats_from_task_rows(rows, now, window_start)
        percentiles = overlap["percentiles"]
        concurrent_peak = overlap["peak"]
        concurrent_p50 = percentiles.get("p50")
        concurrent_p90 = percentiles.get("p90")
        concurrent_p95 = percentiles.get("p95")
        concurrent_p99 = percentiles.get("p99")

        active_task_vms = active_now

        try:
            from app import vm_lifecycle

            managed_vm_count = await vm_lifecycle._get_actual_gcp_vm_count()
        except Exception:  # noqa: BLE001 — best-effort, never blank the snapshot
            managed_vm_count = None

        try:
            from app.warm_pool import (
                count_idle_pool_vms,
                count_orphaned_leased_pool_vms,
                is_warm_pool_enabled,
            )

            _pool_enabled = is_warm_pool_enabled()
            idle_pool_vms = (
                await count_idle_pool_vms(session) if _pool_enabled else 0
            )
            orphaned_leased_vms = (
                await count_orphaned_leased_pool_vms(session) if _pool_enabled else 0
            )
        except Exception:  # noqa: BLE001 — best-effort, never blank the snapshot
            idle_pool_vms = None
            orphaned_leased_vms = None

        if (
            active_task_vms is not None
            and managed_vm_count is not None
            and idle_pool_vms is not None
            and orphaned_leased_vms is not None
            and vm_cap
        ):
            effective = max(
                active_task_vms,
                managed_vm_count - idle_pool_vms - orphaned_leased_vms,
            )
            vm_slots_available = max(0, vm_cap - effective)
            if vm_slots_available == 0:
                vm_saturation = "saturated"
            else:
                ratio = effective / vm_cap
                if ratio < 0.70:
                    vm_saturation = "healthy"
                elif ratio < 0.90:
                    vm_saturation = "busy"
                else:
                    vm_saturation = "saturated"

    return {
        "active_now": active_now,
        "nominal_capacity": nominal_capacity or None,
        "usable_capacity": usable_capacity or None,
        "cooled_accounts": cooled_accounts,
        "util_now": round(util_now, 4) if util_now is not None else None,
        "util_avg": round(util_avg, 4) if util_avg is not None else None,
        "window_hours": window_hours,
        "concurrent_peak": concurrent_peak,
        "concurrent_p50": concurrent_p50,
        "concurrent_p90": concurrent_p90,
        "concurrent_p95": concurrent_p95,
        "concurrent_p99": concurrent_p99,
        "vm_cap": vm_cap or None,
        "active_task_vms": active_task_vms,
        "managed_vm_count": managed_vm_count,
        "idle_pool_vms": idle_pool_vms,
        "orphaned_leased_vms": orphaned_leased_vms,
        "vm_slots_available": vm_slots_available,
        "vm_saturation": vm_saturation,
    }


def compute_headroom_spawn_budget(
    snapshot: Dict[str, Any],
    *,
    min_free_slots: int = 2,
    cap: int = 6,
) -> Dict[str, Any]:
    """Map a fleet snapshot's ``vm_slots_available`` to a research admission budget.

    The autonomous research spawn path previously gated on a binary "is any VM
    slot free?" check (``vm_consuming < max_concurrent_vms``). This helper turns
    the measured headroom into a *budget* — how many research tasks a sweep may
    admit — so admission scales with the fleet instead of being a pass/fail on
    the last slot.

    Returned dict carries the raw inputs plus the resolved ``budget``:

    * ``budget`` — the number of research spawns this tick may admit (0 = none).
    * ``available`` — ``vm_slots_available`` from the snapshot, with a
      task-table-only fallback (``vm_cap - active_task_vms``) when the GCP/warm
      pool read is unavailable; ``None`` only when even the capacity is unknown.
    * ``reason`` — which branch fired: ``headroom_scaled`` (admit up to
      ``min(available, cap)``), ``headroom_below_floor`` (available <
      ``min_free_slots`` → block), or ``headroom_unmeasured`` (nothing
      measurable → fail open to ``cap``).

    Fail-open contract: when headroom cannot be measured at all, the budget is
    ``cap`` so a measurement outage degrades to the old "spawn if any slot is
    free" behavior instead of silently halting research. The per-project
    parallelism caps and the global rate limiter still bound the actual spawn
    count, so the fail-open case is not unbounded.
    """
    available = snapshot.get("vm_slots_available")
    if available is None:
        # vm_slots_available requires the GCP/warm-pool reads (managed count,
        # idle pool, orphaned leases). When those are unavailable, fall back to
        # the task-table-only view the old binary gate already had: capacity
        # minus tasks actively consuming VMs.
        vm_cap = snapshot.get("vm_cap")
        active_task_vms = snapshot.get("active_task_vms")
        if isinstance(vm_cap, int) and isinstance(active_task_vms, int) and vm_cap > 0:
            available = max(0, vm_cap - active_task_vms)
    if available is None:
        return {
            "budget": cap,
            "available": None,
            "cap": cap,
            "min_free_slots": min_free_slots,
            "reason": "headroom_unmeasured",
        }
    if available < min_free_slots:
        return {
            "budget": 0,
            "available": available,
            "cap": cap,
            "min_free_slots": min_free_slots,
            "reason": "headroom_below_floor",
        }
    return {
        "budget": min(available, cap),
        "available": available,
        "cap": cap,
        "min_free_slots": min_free_slots,
        "reason": "headroom_scaled",
    }


# ---------------------------------------------------------------------------
# Recorded fleet-sizing readings
# ---------------------------------------------------------------------------

# Measured per-workspace disk footprint, in bytes. Du-measured 2026-08-06 by
# building a real workspace through the fleet setup path (`git clone --depth
# 100` + `npm ci` at the repo root, `agent/`, and `admin-ui/` plus a backend
# venv) — the per-component breakdown lives in agent/src/workspaceDisk.ts
# DEFAULT_AGENT_DISK_RESERVATION_BYTES. The backend dispatch layer reserves
# the same 2 GiB floor for container-image jobs
# (_CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT in
# orchestrator_runner_dispatch.py), so the agent and backend agree.
MEASURED_PER_WORKSPACE_DISK_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Fleet-sizing target: the number of concurrent task slots the workspace
# footprint must cover. This is the 50-slot claim that once rested on a mocked
# vitest FIFO simulation (agent/src/index.pool-mode.test.ts); the concurrency
# half of the sizing is now measured from the task table (see
# get_recorded_fleet_sizing_readings) and this constant is the slot count the
# disk reservation is scaled to.
FLEET_SIZING_TARGET_SLOTS = 50

# Where the runner-disk half of the footprint reading is sourced from. Kept
# beside the constant so a reader can see the comparison is against the live
# LocalRunner inventory, not a guessed capacity.
RUNNER_DISK_INVENTORY_SOURCE = (
    "LocalRunner.capabilities['disk_free_bytes'] summed across non-deleted "
    "fleet runners (live heartbeat-reported free disk)"
)


def _search_target_concurrency_key() -> dict:
    """Establish whether a 'target concurrency' configuration key exists.

    Criterion c-3fd62b9d asks for monthly spend "at the version's target
    concurrency". This searches the two places such a key could live — the
    runtime config state and the ReleaseVersion model — and reports the
    finding plainly. Today no such key exists (backend/scripts/
    strategic_cycle_cost_projection.py target_concurrency_status documents the
    same gap); running the search live keeps the reading honest rather than
    pinning a hardcoded False.
    """
    from app import release_models, runtime_config_state

    keys = set(runtime_config_state.runtime_config)
    columns = set(release_models.ReleaseVersion.__table__.columns.keys())
    needles = ("target_concurrency", "target_concurrent")
    found = sorted(
        name for name in keys | columns if any(n in name.lower() for n in needles)
    )
    if found:
        return {
            "available": True,
            "keys": found,
            "reason": "a target-concurrency configuration key exists: "
            + ", ".join(found),
        }
    return {
        "available": False,
        "reason": (
            "No 'target_concurrency' configuration key exists for the "
            "strategic decision loop (runtime_config_state.runtime_config "
            "keys and ReleaseVersion columns searched). The monthly "
            "projection therefore reports spend at its available volume "
            "anchors -- the measured realized volume and the cadence-backstop "
            "floor -- and grading c-3fd62b9d at a specific concurrency "
            "requires an operator-supplied target cycle volume."
        ),
    }


async def get_recorded_fleet_sizing_readings(
    session,
    *,
    window_hours: int = 24,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Compose the three fleet-sizing readings the sizing claim rests on.

    This is the durable address for the fleet-sizing numbers: each reading
    reuses an existing instrument rather than a new measurement path.

    * ``concurrency`` — peak + time-weighted percentiles of simultaneous
      task-slot occupancy, taken from ``get_fleet_utilization_snapshot`` (the
      one ``_overlap_stats_from_task_rows`` sweep). A measured peak far below
      ``FLEET_SIZING_TARGET_SLOTS`` is a result to report, not a number to
      manufacture.
    * ``workspace_footprint`` — the du-measured per-workspace disk footprint
      scaled to ``FLEET_SIZING_TARGET_SLOTS`` slots, compared against the live
      runner disk inventory (sum of heartbeat-reported ``disk_free_bytes``
      across non-deleted LocalRunners). ``fits`` is whether the fleet's
      reported free disk can hold the scaled footprint.
    * ``target_concurrency_key`` — whether a 'target concurrency'
      configuration key actually exists (criterion c-3fd62b9d's input). Today
      it does not; the reading reports the absence plainly instead of
      fabricating a volume.

    ``session`` may be None (matching the other snapshot helpers): the
    DB-derived fields then come back as None/zero/empty, while the
    target-concurrency search still runs since it is pure config
    introspection.
    """
    now = _utcnow() if now is None else _naive(now)
    window_hours = max(1, int(window_hours))
    window_start = now - timedelta(hours=window_hours)

    snapshot = await get_fleet_utilization_snapshot(
        session, window_hours=window_hours
    )
    concurrency = {
        "slot_definition": SLOT_DEFINITION,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "window_hours": window_hours,
        "peak": snapshot.get("concurrent_peak"),
        "p50": snapshot.get("concurrent_p50"),
        "p90": snapshot.get("concurrent_p90"),
        "p95": snapshot.get("concurrent_p95"),
        "p99": snapshot.get("concurrent_p99"),
    }

    required_bytes = MEASURED_PER_WORKSPACE_DISK_BYTES * FLEET_SIZING_TARGET_SLOTS
    inventory_bytes = 0
    runner_count = 0
    if session is not None:
        runners = (
            await session.execute(
                select(LocalRunner).where(LocalRunner.status != "deleted")
            )
        ).scalars().all()
        runner_count = len(runners)
        inventory_bytes = sum(
            int(r.capabilities.get("disk_free_bytes") or 0)
            for r in runners
            if isinstance(r.capabilities, dict)
        )
    workspace_footprint = {
        "per_workspace_bytes": MEASURED_PER_WORKSPACE_DISK_BYTES,
        "target_slots": FLEET_SIZING_TARGET_SLOTS,
        "required_bytes": required_bytes,
        "runner_inventory_bytes": inventory_bytes if session is not None else None,
        "runner_count": runner_count if session is not None else None,
        "fits": inventory_bytes >= required_bytes if session is not None else None,
        "shortfall_bytes": (
            max(0, required_bytes - inventory_bytes)
            if session is not None
            else None
        ),
        "source": RUNNER_DISK_INVENTORY_SOURCE,
    }

    return {
        "workspace_footprint": workspace_footprint,
        "concurrency": concurrency,
        "target_concurrency_key": _search_target_concurrency_key(),
    }


async def record_fleet_sizing_reading(
    session,
    *,
    window_hours: int = 24,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Compute and persist one fleet-sizing reading with provenance.

    This is the production trigger for ``get_recorded_fleet_sizing_readings``:
    it computes the three readings from LIVE data (task-table concurrency
    overlap + LocalRunner disk inventory) and appends a ``FleetSizingReading``
    row annotated with *when* it was measured (``captured_at``) and *which*
    deployed backend code computed it (``serving_sha``) — the provenance that
    ties each persisted number to the data it was derived from.

    The caller owns the session's commit. The returned dict is the persisted
    blob (readings + provenance) so a caller can log/return it without an extra
    read.
    """
    now = _utcnow() if now is None else _naive(now)
    assert now is not None  # _utcnow() is a datetime; _naive() only strips tzinfo
    readings = await get_recorded_fleet_sizing_readings(
        session, window_hours=window_hours, now=now
    )
    from app.deployed_sha import get_serving_sha

    serving_sha = get_serving_sha()
    blob = {
        **readings,
        "provenance": {
            "captured_at": now.isoformat(),
            "serving_sha": serving_sha,
            "source": (
                "computed from live task-table slot overlap + LocalRunner "
                "disk inventory via get_recorded_fleet_sizing_readings"
            ),
        },
    }
    row = FleetSizingReading(
        captured_at=now,
        serving_sha=serving_sha,
        window_start=now - timedelta(hours=max(1, int(window_hours))),
        window_end=now,
        blob=blob,
    )
    session.add(row)
    return blob


async def read_latest_fleet_sizing_reading(session) -> Dict[str, Any] | None:
    """Return the most recently persisted fleet-sizing reading, or None.

    The returned dict is the same shape ``record_fleet_sizing_reading``
    persisted (the three readings plus ``provenance``), so a caller can render
    the measured values and their provenance together. ``None`` when no reading
    has been recorded yet.
    """
    row = (
        await session.execute(
            select(FleetSizingReading)
            .order_by(FleetSizingReading.captured_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    return dict(row.blob or {})


# Cadence for the leader-only fleet-sizing reading recorder. Kept beside the
# record function so the measurement cadence and the record path stay together.
FLEET_SIZING_READING_INTERVAL_SECONDS = 15 * 60  # 15 minutes


async def fleet_sizing_reading_service() -> None:
    """Leader-only periodic recorder of fleet-sizing readings.

    Computes a fleet-sizing reading from live data every 15 minutes and
    persists it with provenance, independent of when a strategic cycle happens
    to run — so the fleet-sizing clause has a durable measured record as long
    as the deployed backend is up. Failures are logged and skipped; the
    measurement must never take down the background loop.
    """
    from app.database import AsyncSessionLocal

    while True:
        await asyncio.sleep(FLEET_SIZING_READING_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as session:
                await record_fleet_sizing_reading(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[FLEET_SIZING] reading record failed; skipping tick")
