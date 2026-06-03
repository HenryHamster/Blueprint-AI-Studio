"""Unit tests for the global fleet-utilization signal.

Covers the pure formatter and occupancy helpers plus the capacity/cooled
aggregation in ``get_fleet_utilization_snapshot`` (``session=None`` path — no
DB). The DB-derived ``active_now`` / ``util_now`` are exercised via the pure
occupancy helper rather than a live session so these stay standalone.
"""

from datetime import datetime, timedelta

import pytest

from app import max_plan_pool
from app.fleet_utilization import (
    _sum_slot_occupancy_seconds,
    get_fleet_utilization_snapshot,
)
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import TaskStatus
from app.strategic_spawning import _format_fleet_utilization_axis


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
