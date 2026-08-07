"""Tests for task-table concurrency-overlap measurement.

The fleet-sizing claim (50 slots) must be evidenced by concurrency measured
from the task table — a real interval-overlap sweep over each task's
slot-occupancy interval — not by a mocked scheduler simulation. These tests
exercise the pure sweep (``compute_concurrency_overlap``), the task-row →
interval derivation (``_overlap_stats_from_task_rows``), the shared occupancy
interval helper (``_task_occupancy_interval``), and the DB-backed snapshot
(``get_task_concurrency_snapshot``) on synthetic rows with known overlaps.
"""

from datetime import datetime, timedelta

import pytest

from app.fleet_utilization import (
    _overlap_stats_from_task_rows,
    _task_occupancy_interval,
    compute_concurrency_overlap,
    get_task_concurrency_snapshot,
)
from app.models import TaskStatus


def _slot_row(
    *,
    created_at,
    dispatched_at,
    stopped_at,
    status=TaskStatus.COMPLETED,
    provisioned_at=None,
    updated_at=None,
):
    """Build a task row tuple in the shape the occupancy helpers consume.

    ``(status, created_at, updated_at, vm_dispatched_at, vm_provisioned_at,
    vm_stopped_at)``. For a terminal task ``updated_at`` defaults to the stop
    time (the row was last touched when it finished).
    """
    return (
        status,
        created_at,
        updated_at if updated_at is not None else stopped_at,
        dispatched_at,
        provisioned_at,
        stopped_at,
    )


def test_overlap_computed_from_task_table():
    """Synthetic task rows with known overlaps → correct peak concurrency.

    Covers the boundary cases the fleet-sizing claim must get right: an empty
    table, a single slot-occupying task, exactly-abutting intervals (end ==
    start must NOT count as overlap), fully-nested intervals, and a mixed
    workload peaking at 3.
    """
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)

    # ── Zero tasks: nothing ever occupies a slot → peak 0.
    empty = _overlap_stats_from_task_rows([], now, window_start)
    assert empty["peak"] == 0
    assert empty["task_count"] == 0
    assert empty["concurrency_histogram"] == {}

    # ── One task: a single completed task occupying a slot for 1h.
    single = _overlap_stats_from_task_rows(
        [
            _slot_row(
                created_at=now - timedelta(hours=3),
                dispatched_at=now - timedelta(hours=2),
                stopped_at=now - timedelta(hours=1),
            )
        ],
        now,
        window_start,
    )
    assert single["peak"] == 1
    assert single["task_count"] == 1
    assert single["concurrency_histogram"] == {"1": 3600.0}

    # ── Exactly-abutting intervals: A ends exactly when B starts.
    #    [10:00, 11:00) and [11:00, 12:00) must peak at 1, NOT 2.
    abutting = _overlap_stats_from_task_rows(
        [
            _slot_row(
                created_at=now - timedelta(hours=3),
                dispatched_at=now - timedelta(hours=2),
                stopped_at=now - timedelta(hours=1),
            ),
            _slot_row(
                created_at=now - timedelta(hours=1),
                dispatched_at=now - timedelta(hours=1),
                stopped_at=now,
            ),
        ],
        now,
        window_start,
    )
    assert abutting["peak"] == 1
    assert abutting["concurrency_histogram"] == {"1": 7200.0}

    # ── Fully-nested: the inner interval sits entirely inside the outer.
    #    [09:00, 11:00) and [10:00, 11:00) → peak 2, never 1.
    nested = _overlap_stats_from_task_rows(
        [
            _slot_row(
                created_at=now - timedelta(hours=4),
                dispatched_at=now - timedelta(hours=3),
                stopped_at=now - timedelta(hours=1),
            ),
            _slot_row(
                created_at=now - timedelta(hours=2),
                dispatched_at=now - timedelta(hours=2),
                stopped_at=now - timedelta(hours=1),
            ),
        ],
        now,
        window_start,
    )
    assert nested["peak"] == 2
    assert nested["concurrency_histogram"] == {"1": 3600.0, "2": 3600.0}

    # ── Mixed overlap: A[4,8) B[5,9) C[7,10) D[9,11) E[9.5,10.5) → peak 3.
    mixed = _overlap_stats_from_task_rows(
        [
            _slot_row(
                created_at=now - timedelta(hours=9),
                dispatched_at=now - timedelta(hours=8),
                stopped_at=now - timedelta(hours=4),
            ),
            _slot_row(
                created_at=now - timedelta(hours=8),
                dispatched_at=now - timedelta(hours=7),
                stopped_at=now - timedelta(hours=3),
            ),
            _slot_row(
                created_at=now - timedelta(hours=6),
                dispatched_at=now - timedelta(hours=5),
                stopped_at=now - timedelta(hours=2),
            ),
            _slot_row(
                created_at=now - timedelta(hours=4),
                dispatched_at=now - timedelta(hours=3),
                stopped_at=now - timedelta(hours=1),
            ),
            _slot_row(
                created_at=now - timedelta(hours=3),
                dispatched_at=now - timedelta(hours=2.5),
                stopped_at=now - timedelta(hours=1.5),
            ),
        ],
        now,
        window_start,
    )
    assert mixed["peak"] == 3
    assert mixed["task_count"] == 5
    assert mixed["concurrency_histogram"] == {
        "1": 5400.0,
        "2": 14400.0,
        "3": 5400.0,
    }
    # Over the 7h active window (4:00→11:00) the fleet was at ≥2 concurrency
    # for 5.5h and at ≥3 for 1.5h — the distribution, not just the peak.
    assert mixed["percentiles"]["p50"] == 2
    assert mixed["percentiles"]["p90"] == 3
    assert mixed["percentiles"]["p100"] == 3


def test_compute_concurrency_overlap_abutting_three_tasks():
    """A chain of abutting intervals never overlaps at any boundary."""
    now = datetime(2026, 6, 2, 12, 0, 0)

    def at(hours_ago):
        return now - timedelta(hours=hours_ago)

    stats = compute_concurrency_overlap(
        [(at(3), at(2)), (at(2), at(1)), (at(1), at(0))]
    )
    assert stats["peak"] == 1
    assert stats["task_count"] == 3
    assert stats["concurrency_histogram"] == {"1": 10800.0}


def test_task_occupancy_interval_terminal_without_vm_stamp_is_none():
    """Terminal tasks that never recorded a VM stamp never held a slot."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    assert (
        _task_occupancy_interval(
            TaskStatus.FAILED,
            now - timedelta(hours=3),
            now - timedelta(hours=3),
            None,
            None,
            None,
            now,
        )
        is None
    )


def test_task_occupancy_interval_active_falls_back_to_created_and_now():
    """A still-active task with no VM stamps spans created_at → now."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    start, end = _task_occupancy_interval(
        TaskStatus.IN_PROGRESS,
        now - timedelta(hours=2),
        now - timedelta(hours=2),
        None,
        None,
        None,
        now,
    )
    assert start == now - timedelta(hours=2)
    assert end == now


def test_overlap_clips_intervals_to_window():
    """Tasks straddling the window edge are clipped, not dropped or inflated."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    window_start = now - timedelta(hours=24)
    # Started 6h before the window, stopped 4h inside it → 4h counted.
    stats = _overlap_stats_from_task_rows(
        [
            _slot_row(
                created_at=now - timedelta(hours=30),
                dispatched_at=now - timedelta(hours=30),
                stopped_at=now - timedelta(hours=20),
            )
        ],
        now,
        window_start,
    )
    assert stats["peak"] == 1
    assert stats["window_seconds"] == pytest.approx(4 * 3600)
    assert stats["concurrency_histogram"] == {"1": pytest.approx(14400.0)}


@pytest.mark.asyncio
async def test_task_concurrency_snapshot_from_db(db_session):
    """get_task_concurrency_snapshot measures real task-table rows end-to-end."""
    from testing.factories import make_project, make_task

    now = datetime(2026, 6, 2, 12, 0, 0)
    project = make_project(name="Concurrency Overlap Test Project")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A: still-active, dispatched 10:00 → spans to now (12:00).
    # B: [10:30, 11:30). C: [11:00, 11:30). Peak = 3 over 11:00–11:30.
    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.IN_PROGRESS,
                created_at=now - timedelta(hours=3),
                updated_at=now - timedelta(hours=2),
                vm_dispatched_at=now - timedelta(hours=2),
                vm_provisioned_at=now - timedelta(hours=2),
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=0.5),
                vm_dispatched_at=now - timedelta(hours=1.5),
                vm_provisioned_at=now - timedelta(hours=1.5),
                vm_stopped_at=now - timedelta(hours=0.5),
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                created_at=now - timedelta(hours=1.5),
                updated_at=now - timedelta(hours=0.5),
                vm_dispatched_at=now - timedelta(hours=1),
                vm_provisioned_at=now - timedelta(hours=1),
                vm_stopped_at=now - timedelta(hours=0.5),
            ),
        ]
    )
    await db_session.commit()

    snap = await get_task_concurrency_snapshot(db_session, now=now)
    assert snap["peak"] == 3
    assert snap["task_count"] == 3
    assert snap["window_hours"] == 24
    assert snap["window_start"] == (now - timedelta(hours=24)).isoformat()
    assert snap["window_end"] == now.isoformat()
    assert snap["slot_definition"]


@pytest.mark.asyncio
async def test_task_concurrency_snapshot_without_session_returns_zeros():
    """session=None → all measured fields are zero/empty (mirrors the
    fleet-utilization snapshot contract)."""
    now = datetime(2026, 6, 2, 12, 0, 0)
    snap = await get_task_concurrency_snapshot(None, now=now)
    assert snap["peak"] == 0
    assert snap["task_count"] == 0
    assert snap["concurrency_histogram"] == {}
    assert snap["percentiles"] == {}
