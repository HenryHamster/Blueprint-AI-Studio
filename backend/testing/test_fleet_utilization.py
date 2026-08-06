"""Unit tests for the global fleet-utilization signal and fleet-wide runner-health summary.

Covers the pure formatter and occupancy helpers plus the capacity/cooled
aggregation in ``get_fleet_utilization_snapshot`` (``session=None`` path — no
DB). The DB-derived ``active_now`` / ``util_now`` are exercised via the pure
occupancy helper rather than a live session so these stay standalone.

The fleet-wide runner-health summary endpoint
(``GET /api/admin/fleet/runners/health-summary``) is exercised with
in-memory LocalRunner rows through the async test client, verifying that
mixed healthy/degraded/offline/low-disk runners across >=2 projects roll up
correctly in the aggregate counts.
"""

from datetime import datetime, timedelta

import pytest
import uuid

from app import max_plan_pool
from app.fleet_utilization import (
    _overlap_stats_from_task_rows,
    _sum_slot_occupancy_seconds,
    get_fleet_utilization_snapshot,
)
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import LocalRunner, Project, ProjectMember, TaskStatus, _utcnow
from app.strategic_spawning import _format_fleet_utilization_axis

TEST_USER_ID = "test-user-fleet-health"


@pytest.fixture(autouse=True)
def _clean_pool():
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()


# ── _format_fleet_utilization_axis ──────────────────────────────────────────


def test_format_none_when_no_capacity():
    assert _format_fleet_utilization_axis(None) is None
    assert _format_fleet_utilization_axis({}) is None
    assert _format_fleet_utilization_axis({"nominal_capacity": 0}) is None
    assert _format_fleet_utilization_axis({"nominal_capacity": None}) is None


def test_format_basic_now_and_avg():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 6,
            "nominal_capacity": 15,
            "util_now": 0.4,
            "util_avg": 0.38,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
        }
    )
    assert line == "now 6/15 (40%), 24h-avg 38%"


def test_format_appends_cooled_breakdown_only_when_cooled():
    cooled = _format_fleet_utilization_axis(
        {
            "active_now": 6,
            "nominal_capacity": 15,
            "util_now": 0.4,
            "util_avg": 0.38,
            "window_hours": 24,
            "cooled_accounts": 2,
            "usable_capacity": 9,
        }
    )
    assert "2 acct cooled, 9 usable" in cooled

    clean = _format_fleet_utilization_axis(
        {
            "active_now": 6,
            "nominal_capacity": 15,
            "util_now": 0.4,
            "util_avg": 0.38,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
        }
    )
    assert "cooled" not in clean


def test_format_handles_missing_avg():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 0,
            "nominal_capacity": 15,
            "util_now": 0.0,
            "util_avg": None,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
        }
    )
    assert line == "now 0/15 (0%)"


def test_format_respects_window_hours():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 3,
            "nominal_capacity": 15,
            "util_now": 0.2,
            "util_avg": 0.25,
            "window_hours": 12,
            "cooled_accounts": 0,
            "usable_capacity": 15,
        }
    )
    assert "12h-avg 25%" in line


def test_format_appends_concurrency_segment_when_present():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 6,
            "nominal_capacity": 15,
            "util_now": 0.4,
            "util_avg": 0.38,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
            "concurrent_peak": 9,
            "concurrent_p90": 7,
        }
    )
    assert line == "now 6/15 (40%), 24h-avg 38%, peak 9, p90 7"


def test_format_omits_concurrency_segment_when_absent():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 6,
            "nominal_capacity": 15,
            "util_now": 0.4,
            "util_avg": 0.38,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
        }
    )
    assert line == "now 6/15 (40%), 24h-avg 38%"
    assert "peak" not in line


# ── _sum_slot_occupancy_seconds ─────────────────────────────────────────────


def test_occupancy_clips_to_window_counts_active_to_now_and_skips_no_slot():
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    rows = [
        # Terminal task fully inside window: 1h occupancy.
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=5),
            now - timedelta(hours=4),
            now - timedelta(hours=5),
            None,
            now - timedelta(hours=4),
        ),
        # Still-active task started 2h ago, no stop -> counts to now (2h).
        (
            TaskStatus.IN_PROGRESS,
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            None,
            None,
        ),
        # Terminal task that never recorded a VM stamp -> never held a slot -> 0.
        (
            TaskStatus.FAILED,
            now - timedelta(hours=3),
            now - timedelta(hours=3),
            None,
            None,
            None,
        ),
        # Task started before the window -> clipped to window_start (4h in window).
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=30),
            now - timedelta(hours=20),
            now - timedelta(hours=30),
            None,
            now - timedelta(hours=20),
        ),
    ]
    secs = _sum_slot_occupancy_seconds(rows, now, window_start)
    assert secs == pytest.approx((1 + 2 + 4) * 3600)


def test_occupancy_falls_back_to_provisioned_then_created_for_active():
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    rows = [
        # No dispatched stamp, but provisioned 1h ago and still active -> 1h.
        (
            TaskStatus.PROVISIONING,
            now - timedelta(hours=3),
            now - timedelta(hours=1),
            None,
            now - timedelta(hours=1),
            None,
        ),
    ]
    assert _sum_slot_occupancy_seconds(rows, now, window_start) == pytest.approx(3600)


# ── measured slot concurrency (shared _overlap_stats_from_task_rows sweep) ───
#
# The fleet snapshot's concurrent_* fields are derived from the SAME sweep
# get_task_concurrency_snapshot uses, so its denominator applies here: seconds
# are weighted over the ACTIVE window (first slot start .. last slot end,
# interior idle gaps at level 0), not over the full window_hours.


def _completed_row(now, dispatched_h, stopped_h):
    """Build a terminal COMPLETED row that held a slot ``[dispatched, stopped)``.

    ``dispatched_h`` / ``stopped_h`` are offsets in hours BEFORE ``now``.
    """

    def _at(h):
        return now - timedelta(hours=h)

    return (
        TaskStatus.COMPLETED,
        _at(dispatched_h),
        _at(stopped_h),
        _at(dispatched_h),
        None,
        _at(stopped_h),
    )


def test_peak_concurrency_from_overlapping_intervals():
    """Peak concurrent slot overlap is recovered from synthetic interval rows.

    Builds a KNOWN overlap structure: an overlapping cluster peaking at 3, a
    staggered tail with a brief overlap at 2, a task ending exactly when
    another starts (half-open intervals -> the shared instant must NOT count
    as overlap), and an empty input (peak 0 / empty percentiles).
    """
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)

    # Overlapping cluster -> peak 3 at [3.5h, 3h) ago.
    cluster = [
        _completed_row(now, 5, 3),
        _completed_row(now, 4, 2),
        _completed_row(now, 3.5, 1.5),
    ]

    # Staggered tail -> brief overlap at 2, then a lone task.
    tail = [
        _completed_row(now, 8, 7),
        _completed_row(now, 7.5, 6.5),
        _completed_row(now, 6, 5.5),
    ]

    # A task ending exactly when another starts: the shared instant must NOT
    # count as overlap (ends sort before starts at equal timestamps).
    abutting = [
        _completed_row(now, 10, 9),
        _completed_row(now, 9, 8.5),
    ]

    stats = _overlap_stats_from_task_rows(cluster + tail + abutting, now, window_start)
    assert stats["peak"] == 3

    # The abutting pair alone must yield peak 1, not 2.
    abutting_stats = _overlap_stats_from_task_rows(abutting, now, window_start)
    assert abutting_stats["peak"] == 1

    # Empty input -> sane defaults (peak 0, no percentiles to report).
    empty = _overlap_stats_from_task_rows([], now, window_start)
    assert empty["peak"] == 0
    assert empty["percentiles"] == {}


def test_sweep_time_weighted_percentiles():
    """Percentiles are time-weighted over the ACTIVE window, not count-weighted.

    1 slot occupied for 3h, then a 1h idle gap, then 2 slots for 1h. The active
    window is first-start..last-end = 5h, so leading/trailing idle in the 24h
    query window is NOT counted: level 0 holds 1h (20%), level 1 holds 3h
    (cum 80%), level 2 holds 1h (cum 100%). p50 -> 1, p90/p95/p99 -> 2.
    """
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    rows = [
        _completed_row(now, 20, 17),  # 1 slot for 3h
        _completed_row(now, 16, 15),  # 2 slots for 1h
        _completed_row(now, 16, 15),
    ]
    stats = _overlap_stats_from_task_rows(rows, now, window_start)
    assert stats["peak"] == 2
    assert stats["window_seconds"] == pytest.approx(5 * 3600)
    pcts = stats["percentiles"]
    assert pcts["p50"] == 1
    assert pcts["p90"] == 2
    assert pcts["p95"] == 2
    assert pcts["p99"] == 2
    assert pcts["p100"] == 2


def test_sweep_active_task_counts_to_now():
    """A still-active task's interval extends to now (no stop stamp)."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    rows = [
        (
            TaskStatus.IN_PROGRESS,
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            now - timedelta(hours=2),
            None,
            None,
        ),
    ]
    stats = _overlap_stats_from_task_rows(rows, now, window_start)
    assert stats["peak"] == 1
    # The active window is exactly the task's 2h, all at level 1 — trailing
    # idle in the 24h query window is outside the denominator.
    assert stats["window_seconds"] == pytest.approx(2 * 3600)
    assert stats["percentiles"]["p50"] == 1
    assert stats["percentiles"]["p90"] == 1
    assert stats["percentiles"]["p95"] == 1
    assert stats["percentiles"]["p99"] == 1


def test_sweep_clips_to_window_and_skips_no_slot():
    """Intervals are clipped to the window; terminal no-slot tasks are skipped."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    rows = [
        # Started before the window, stopped inside -> clipped to window_start.
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=30),
            now - timedelta(hours=20),
            now - timedelta(hours=30),
            None,
            now - timedelta(hours=20),
        ),
        # Terminal task that never recorded a VM stamp -> never held a slot.
        (
            TaskStatus.FAILED,
            now - timedelta(hours=3),
            now - timedelta(hours=3),
            None,
            None,
            None,
        ),
    ]
    stats = _overlap_stats_from_task_rows(rows, now, window_start)
    assert stats["peak"] == 1
    assert stats["task_count"] == 1
    # Clipped at window_start: 20h of the 30h-old task, not 30h.
    assert stats["window_seconds"] == pytest.approx(4 * 3600)


# ── get_fleet_utilization_snapshot capacity aggregation (no DB) ──────────────


async def test_capacity_aggregation_excludes_disabled_and_uncapped(monkeypatch):
    accts = [
        {"id": "1", "enabled": True, "max_concurrent_tasks": 3, "_avail": True},
        {"id": "2", "enabled": True, "max_concurrent_tasks": 3, "_avail": True},
        {"id": "3", "enabled": True, "max_concurrent_tasks": 3, "_avail": False},
        {"id": "4", "enabled": False, "max_concurrent_tasks": 3, "_avail": True},
        {"id": "5", "enabled": True, "max_concurrent_tasks": None, "_avail": True},
    ]
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: accts)
    monkeypatch.setattr(
        max_plan_pool,
        "_account_available_for_model",
        lambda acct, model: acct.get("_avail", True),
    )
    snap = await get_fleet_utilization_snapshot(session=None)
    assert snap["nominal_capacity"] == 9  # accts 1,2,3 enabled+capped
    assert snap["usable_capacity"] == 6  # accts 1,2 available; 3 cooled
    assert snap["cooled_accounts"] == 1
    assert snap["active_now"] == 0  # session=None -> unmeasured
    assert snap["util_now"] is None
    assert snap["util_avg"] is None
    assert snap["window_hours"] == 24


async def test_zero_capacity_yields_none(monkeypatch):
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    snap = await get_fleet_utilization_snapshot(session=None)
    assert snap["nominal_capacity"] is None
    assert snap["usable_capacity"] is None
    assert snap["util_now"] is None
    assert snap["util_avg"] is None
    assert snap["cooled_accounts"] == 0


async def test_snapshot_concurrency_fields_none_without_session(monkeypatch):
    """session=None leaves the measured concurrency fields None."""
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    snap = await get_fleet_utilization_snapshot(session=None)
    assert snap["concurrent_peak"] is None
    assert snap["concurrent_p50"] is None
    assert snap["concurrent_p90"] is None
    assert snap["concurrent_p95"] is None
    assert snap["concurrent_p99"] is None


async def test_snapshot_surfaces_concurrency_fields_with_session(monkeypatch):
    """With a mock session the concurrency sweep is surfaced in the snapshot."""
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 10)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    monkeypatch.setattr(
        max_plan_pool, "_account_available_for_model", lambda acct, model: True
    )
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 6)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: False)

    now = _utcnow()
    rows = [
        # Overlapping cluster -> peak 3.
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=5),
            now - timedelta(hours=3),
            now - timedelta(hours=5),
            None,
            now - timedelta(hours=3),
        ),
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=4),
            now - timedelta(hours=2),
            now - timedelta(hours=4),
            None,
            now - timedelta(hours=2),
        ),
        (
            TaskStatus.COMPLETED,
            now - timedelta(hours=3.5),
            now - timedelta(hours=1.5),
            now - timedelta(hours=3.5),
            None,
            now - timedelta(hours=1.5),
        ),
    ]

    class _FakeResult:
        def scalar(self):
            return 3

        def all(self):
            return rows

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["concurrent_peak"] == 3
    assert snap["concurrent_p50"] is not None
    assert snap["concurrent_p90"] is not None
    assert snap["concurrent_p95"] is not None
    assert snap["concurrent_p99"] is not None


# ── VM-slot fields ───────────────────────────────────────────────────────────


async def test_vm_cap_surfaced_without_session(monkeypatch):
    """vm_cap is available even when session=None; DB-derived VM fields are None."""
    import app.runtime_config_state as rcs

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 12)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    snap = await get_fleet_utilization_snapshot(session=None)
    assert snap["vm_cap"] == 12
    assert snap["active_task_vms"] is None
    assert snap["managed_vm_count"] is None
    assert snap["idle_pool_vms"] is None
    assert snap["vm_slots_available"] is None
    assert snap["vm_saturation"] is None


async def test_vm_slot_fields_with_session(monkeypatch):
    """With a mock session the VM-slot derivation math is exercised end-to-end."""
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 10)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    monkeypatch.setattr(
        max_plan_pool, "_account_available_for_model", lambda acct, model: True
    )

    # managed=6, idle_pool=1 → effective = max(3, 6-1)=5; slots_available=5
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 6)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: True)

    class _FakeResult:
        def scalar(self):
            return 3

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, stmt):
            # First call returns active count, second returns rows for occupancy
            return _FakeResult()

    async def _fake_idle(session, **kw):
        return 1

    async def _fake_orphaned(session, **kw):
        return 0

    monkeypatch.setattr(warm_pool, "count_idle_pool_vms", _fake_idle)
    monkeypatch.setattr(warm_pool, "count_orphaned_leased_pool_vms", _fake_orphaned)

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["vm_cap"] == 10
    assert snap["active_task_vms"] == 3
    assert snap["managed_vm_count"] == 6
    assert snap["idle_pool_vms"] == 1
    assert snap["orphaned_leased_vms"] == 0
    # effective = max(3, 6-1-0) = 5; slots_available = 10-5 = 5
    assert snap["vm_slots_available"] == 5
    assert snap["vm_saturation"] == "healthy"  # 5/10 = 0.5 < 0.70


async def test_vm_saturation_saturated_when_slots_zero(monkeypatch):
    """vm_saturation is 'saturated' when vm_slots_available == 0."""
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 5)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])

    # managed=5, idle=0 → effective=max(5,5)=5; slots=5-5=0
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 5)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: False)

    class _FakeResult:
        def scalar(self):
            return 5

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["vm_slots_available"] == 0
    assert snap["vm_saturation"] == "saturated"


async def test_vm_saturation_busy_threshold(monkeypatch):
    """effective/vm_cap in [0.70, 0.90) → 'busy'."""
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 10)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])

    # active=8, managed=8, idle=0 → effective=8; 8/10=0.80 → busy
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 8)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: False)

    class _FakeResult:
        def scalar(self):
            return 8

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["vm_slots_available"] == 2
    assert snap["vm_saturation"] == "busy"


async def test_vm_saturation_effective_uses_max_of_active_vs_managed(monkeypatch):
    """effective = max(active_task_vms, managed_vm_count - idle_pool_vms)."""
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 10)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])

    # active=9 (high), managed=6, idle=2 → managed-idle=4; effective=max(9,4)=9
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 6)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: True)

    class _FakeResult:
        def scalar(self):
            return 9

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    async def _fake_idle(session, **kw):
        return 2

    async def _fake_orphaned(session, **kw):
        return 0

    monkeypatch.setattr(warm_pool, "count_idle_pool_vms", _fake_idle)
    monkeypatch.setattr(warm_pool, "count_orphaned_leased_pool_vms", _fake_orphaned)

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["vm_slots_available"] == 1  # 10 - 9 = 1
    assert snap["vm_saturation"] == "saturated"  # 9/10 = 0.90 >= 0.90


async def test_vm_slot_fields_exclude_orphaned_leases(monkeypatch):
    """Orphaned LEASED pool rows are subtracted from effective occupancy.

    managed=8, idle=0, orphaned=3 → effective = max(active, 8-0-3) = max(2, 5) = 5;
    vm_slots_available = 10-5 = 5. Without orphan subtraction effective would be 8
    and slots_available would be 2 — understating available capacity.
    """
    import app.runtime_config_state as rcs
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    monkeypatch.setitem(rcs.runtime_config, "max_concurrent_vms", 10)
    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    monkeypatch.setattr(
        max_plan_pool, "_account_available_for_model", lambda acct, model: True
    )

    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 8)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: True)

    class _FakeResult:
        def scalar(self):
            return 2

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

    async def _fake_idle(session, **kw):
        return 0

    async def _fake_orphaned(session, **kw):
        return 3

    monkeypatch.setattr(warm_pool, "count_idle_pool_vms", _fake_idle)
    monkeypatch.setattr(warm_pool, "count_orphaned_leased_pool_vms", _fake_orphaned)

    snap = await get_fleet_utilization_snapshot(session=_FakeSession())
    assert snap["orphaned_leased_vms"] == 3
    assert snap["active_task_vms"] == 2
    assert snap["managed_vm_count"] == 8
    assert snap["idle_pool_vms"] == 0
    # effective = max(2, 8-0-3) = 5; slots_available = 10-5 = 5
    assert snap["vm_slots_available"] == 5
    assert snap["vm_saturation"] == "healthy"  # 5/10 = 0.5 < 0.70


# ── VM-slot segment in strategic axis formatter ──────────────────────────────


def test_format_includes_vm_slot_segment():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 3,
            "nominal_capacity": 15,
            "util_now": 0.2,
            "util_avg": 0.25,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
            "vm_cap": 10,
            "active_task_vms": 3,
            "managed_vm_count": 6,
            "idle_pool_vms": 1,
            "vm_slots_available": 5,
        }
    )
    assert "vm 5/10, 5 free" in line


def test_format_vm_saturated_marker():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 10,
            "nominal_capacity": 15,
            "util_now": 0.67,
            "util_avg": 0.67,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
            "vm_cap": 10,
            "active_task_vms": 10,
            "managed_vm_count": 10,
            "idle_pool_vms": 0,
            "vm_slots_available": 0,
        }
    )
    assert "[SATURATED]" in line
    assert "0 free" in line


def test_format_omits_vm_segment_when_no_vm_cap():
    line = _format_fleet_utilization_axis(
        {
            "active_now": 3,
            "nominal_capacity": 15,
            "util_now": 0.2,
            "util_avg": 0.25,
            "window_hours": 24,
            "cooled_accounts": 0,
            "usable_capacity": 15,
            "vm_cap": None,
        }
    )
    assert "vm" not in line


# ── Fleet-wide runner-health summary endpoint ──────────────────────────────────


def _make_health_runner(
    *,
    project_id: uuid.UUID,
    hostname: str = "test-runner",
    status: str = "active",
    last_seen_at_offset_sec: int = 30,
    consecutive_job_failures: int = 0,
    last_successful_job_at_offset_sec: int | None = 60,
    disk_free_bytes: int | None = None,
    extra_caps: dict | None = None,
) -> LocalRunner:
    """Build an in-memory LocalRunner for fleet-health aggregate tests."""
    now = _utcnow()
    caps = {"task_types": "all", "max_concurrent": 2}
    if disk_free_bytes is not None:
        caps["disk_free_bytes"] = disk_free_bytes
    if extra_caps:
        caps.update(extra_caps)
    return LocalRunner(
        project_id=project_id,
        token_hash="a" * 64,
        hostname=hostname,
        status=status,
        last_seen_at=now - timedelta(seconds=last_seen_at_offset_sec),
        consecutive_job_failures=consecutive_job_failures,
        last_successful_job_at=(
            now - timedelta(seconds=last_successful_job_at_offset_sec)
            if last_successful_job_at_offset_sec is not None
            else None
        ),
        capabilities=caps,
    )


async def _setup_project_with_owner(session) -> Project:
    """Create a project with the test user as owner, return the project."""
    project = Project(name="Fleet Health Test Project", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


class TestFleetRunnersHealthSummary:
    @pytest.mark.asyncio
    async def test_empty_fleet_returns_zero_counts(self, client, db_session):
        """No runners → total=0, all status counts are zero."""
        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["connected"] == 0
        assert data["degraded"] == 0
        assert data["stale"] == 0
        assert data["offline"] == 0
        assert data["low_disk"] == 0
        assert data["project_count"] == 0
        assert data["per_project"] == []
        assert "generated_at" in data

    @pytest.mark.asyncio
    async def test_all_healthy_runners_single_project(self, client, db_session):
        """Three healthy runners in one project → connected=3."""
        project = await _setup_project_with_owner(db_session)
        now = _utcnow()
        for i in range(3):
            runner = LocalRunner(
                project_id=project.id,
                token_hash=f"{i:064x}",
                hostname=f"host-{i}",
                status="active",
                last_seen_at=now - timedelta(seconds=30),
                consecutive_job_failures=0,
                last_successful_job_at=now - timedelta(seconds=60),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
            db_session.add(runner)
        await db_session.commit()

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["connected"] == 3
        assert data["degraded"] == 0
        assert data["stale"] == 0
        assert data["offline"] == 0
        assert data["low_disk"] == 0
        assert data["project_count"] == 1
        assert len(data["per_project"]) == 1
        assert data["per_project"][0]["total"] == 3
        assert data["per_project"][0]["connected"] == 3

    @pytest.mark.asyncio
    async def test_mixed_health_across_two_projects(self, client, db_session):
        """Healthy + degraded + low-disk runners across two projects roll up."""
        p1 = await _setup_project_with_owner(db_session)
        p2 = Project(name="Second Project", user_id=TEST_USER_ID)
        db_session.add(p2)
        await db_session.commit()
        await db_session.refresh(p2)
        db_session.add(ProjectMember(project_id=p2.id, user_id=TEST_USER_ID, role="owner"))
        await db_session.commit()

        now = _utcnow()

        # Project 1: 1 connected, 1 degraded (3 failures), 1 low-disk
        db_session.add(
            LocalRunner(
                project_id=p1.id, token_hash="b" * 64, hostname="p1-ok",
                status="active", last_seen_at=now - timedelta(seconds=30),
                consecutive_job_failures=0,
                last_successful_job_at=now - timedelta(seconds=60),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        db_session.add(
            LocalRunner(
                project_id=p1.id, token_hash="c" * 64, hostname="p1-degraded",
                status="active", last_seen_at=now - timedelta(seconds=30),
                consecutive_job_failures=3,  # >= threshold
                last_successful_job_at=now - timedelta(seconds=60),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        db_session.add(
            LocalRunner(
                project_id=p1.id, token_hash="d" * 64, hostname="p1-lowdisk",
                status="active", last_seen_at=now - timedelta(seconds=30),
                consecutive_job_failures=0,
                last_successful_job_at=now - timedelta(seconds=60),
                capabilities={"task_types": "all", "max_concurrent": 2, "disk_free_bytes": 1024 * 1024},  # 1 MiB << 2 GiB
            )
        )

        # Project 2: 1 stale, 1 offline
        db_session.add(
            LocalRunner(
                project_id=p2.id, token_hash="e" * 64, hostname="p2-stale",
                status="active", last_seen_at=now - timedelta(seconds=600),  # > 300s
                consecutive_job_failures=0, last_successful_job_at=None,
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        db_session.add(
            LocalRunner(
                project_id=p2.id, token_hash="f" * 64, hostname="p2-offline",
                status="inactive", last_seen_at=None,
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )

        await db_session.commit()

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        # Fleet aggregate
        assert data["total"] == 5
        assert data["connected"] == 2  # p1-ok + p1-lowdisk (low disk doesn't change status)
        assert data["degraded"] == 1  # p1-degraded
        assert data["stale"] == 1  # p2-stale
        assert data["offline"] == 1  # p2-offline
        assert data["low_disk"] == 1  # p1-lowdisk
        assert data["project_count"] == 2
        assert len(data["per_project"]) == 2

        # Per-project assertions
        p1_entry = next(e for e in data["per_project"] if e["project_id"] == str(p1.id))
        assert p1_entry["total"] == 3
        assert p1_entry["connected"] == 2
        assert p1_entry["degraded"] == 1
        assert p1_entry["offline"] == 0
        assert p1_entry["low_disk"] == 1

        p2_entry = next(e for e in data["per_project"] if e["project_id"] == str(p2.id))
        assert p2_entry["total"] == 2
        assert p2_entry["connected"] == 0
        assert p2_entry["stale"] == 1
        assert p2_entry["offline"] == 1

    @pytest.mark.asyncio
    async def test_skips_deleted_runners(self, client, db_session):
        """Deleted runners are excluded from the fleet summary."""
        project = await _setup_project_with_owner(db_session)
        now = _utcnow()

        db_session.add(
            LocalRunner(
                project_id=project.id, token_hash="g" * 64, hostname="active",
                status="active", last_seen_at=now - timedelta(seconds=30),
                consecutive_job_failures=0,
                last_successful_job_at=now - timedelta(seconds=60),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        db_session.add(
            LocalRunner(
                project_id=project.id, token_hash="h" * 64, hostname="deleted",
                status="deleted", last_seen_at=None,
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["project_count"] == 1

    @pytest.mark.asyncio
    async def test_success_stale_while_working_marks_degraded(self, client, db_session):
        """Runner live with active work, no recent success, some failures → degraded."""
        project = await _setup_project_with_owner(db_session)
        now = _utcnow()

        # Runner with active work, 1 failure, stale success (>30 min)
        runner = LocalRunner(
            project_id=project.id,
            token_hash="i" * 64,
            hostname="busy-failing",
            status="active",
            last_seen_at=now - timedelta(seconds=30),
            consecutive_job_failures=1,
            last_successful_job_at=now - timedelta(seconds=3600),  # 1 hour ago > 30 min
            capabilities={"task_types": "all", "max_concurrent": 2, "active_task_ids": ["task-1"]},
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["degraded"] == 1
        assert data["connected"] == 0

    @pytest.mark.asyncio
    async def test_per_project_runners_include_health(self, client, db_session):
        """Each runner entry in the per-project list includes its health dict."""
        project = await _setup_project_with_owner(db_session)
        now = _utcnow()
        runner = LocalRunner(
            project_id=project.id,
            token_hash="j" * 64,
            hostname="health-check",
            status="active",
            last_seen_at=now - timedelta(seconds=30),
            consecutive_job_failures=0,
            last_successful_job_at=now - timedelta(seconds=60),
            capabilities={"task_types": "all", "max_concurrent": 2, "disk_free_bytes": 50 * 1024**3},
        )
        db_session.add(runner)
        await db_session.commit()

        resp = await client.get("/api/admin/fleet/runners/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["per_project"][0]["runners"]) == 1
        r = data["per_project"][0]["runners"][0]
        assert r["hostname"] == "health-check"
        assert "health" in r
        assert r["health"]["device_status"] == "connected"
        assert r["health"]["disk_low"] is False
        assert r["health"]["degraded"] is False
