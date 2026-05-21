"""Tests for backend/app/efficiency_snapshot.py and the admin endpoint."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.efficiency_snapshot import (
    ALL_AXES,
    AXIS_COST_PER_MERGED_PR,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    DESIGN_DOC_KEY,
    _last_run_due,
    compute_current_axes,
    maybe_write_weekly_snapshot,
    read_baseline_with_drift,
    write_snapshot,
)
from app.models import TaskStatus
from app.routers.admin_analytics import ON_DEMAND_RATE
from testing.factories import _utcnow, make_membership, make_project, make_task


def _make_efficient_task(project_id, **overrides):
    """Create a COMPLETED + merged task with VM cost recorded."""
    now = _utcnow()
    defaults = dict(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "review_loop": {"iteration": 2}},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
        vm_cold_start_ms=12000,
        vm_warm_start=False,
        api_call_summary={"llm_call_count": 40},
    )
    defaults.update(overrides)
    return make_task(**defaults)


@pytest.mark.asyncio
async def test_compute_current_axes_returns_all_keys(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    db_session.add(_make_efficient_task(project.id, vm_warm_start=True))
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    for key in ALL_AXES:
        assert key in axes


@pytest.mark.asyncio
async def test_compute_current_axes_values(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two merged tasks, each 5min on-demand. 1 cold (12s), 1 warm (no cold sample).
    db_session.add(_make_efficient_task(project.id))
    db_session.add(
        _make_efficient_task(
            project.id,
            vm_warm_start=True,
            vm_cold_start_ms=None,
            api_call_summary={"llm_call_count": 80},
            context={"pr_merged": True, "review_loop": {"iteration": 4}},
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # 2 merged * 300s * ON_DEMAND_RATE / 2 merged = 300 * ON_DEMAND_RATE
    expected_cpm = round(300 * ON_DEMAND_RATE, 4)
    assert axes[AXIS_COST_PER_MERGED_PR] == expected_cpm
    assert axes[AXIS_VM_COLD_START_P50_MS] == 12000.0
    assert axes[AXIS_VM_REUSE_RATE] == 0.5
    assert axes[AXIS_MEDIAN_API_CALLS_PER_TASK] == 60.0
    assert axes[AXIS_MEDIAN_REVIEW_ITERATIONS] == 3.0


@pytest.mark.asyncio
async def test_compute_current_axes_empty_returns_none(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    axes = await compute_current_axes(db_session, project.id)
    assert all(v is None for v in axes.values())


@pytest.mark.asyncio
async def test_first_snapshot_seeds_baseline(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY)
    assert container is not None
    assert container["baseline"]["captured_at"] == snap["captured_at"]
    assert (
        container["baseline"][AXIS_COST_PER_MERGED_PR] == snap[AXIS_COST_PER_MERGED_PR]
    )
    assert len(container["snapshots"]) == 1


@pytest.mark.asyncio
async def test_subsequent_snapshots_do_not_clobber_baseline(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    first = await write_snapshot(db_session, project.id)
    await db_session.commit()

    # Add a second, more expensive task and re-snapshot.
    db_session.add(
        _make_efficient_task(
            project.id,
            vm_provisioned_at=_utcnow() - timedelta(minutes=20),
            vm_stopped_at=_utcnow(),
        )
    )
    await db_session.commit()

    second = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    # Baseline frozen at first snapshot's value.
    assert (
        container["baseline"][AXIS_COST_PER_MERGED_PR] == first[AXIS_COST_PER_MERGED_PR]
    )
    # Latest snapshot reflects new (higher) cost.
    assert (
        container["snapshots"][-1][AXIS_COST_PER_MERGED_PR]
        == second[AXIS_COST_PER_MERGED_PR]
    )
    assert len(container["snapshots"]) == 2


@pytest.mark.asyncio
async def test_read_baseline_with_drift_returns_drift_pct(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    # Mutate the latest snapshot in-place to simulate a regression of +50%.
    container = dict(project.design_doc[DESIGN_DOC_KEY])
    snaps = list(container["snapshots"])
    latest = dict(snaps[-1])
    base_cpm = container["baseline"][AXIS_COST_PER_MERGED_PR]
    latest[AXIS_COST_PER_MERGED_PR] = round(base_cpm * 1.5, 6)
    snaps[-1] = latest
    container["snapshots"] = snaps
    new_doc = dict(project.design_doc)
    new_doc[DESIGN_DOC_KEY] = container
    project.design_doc = new_doc
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()

    result = await read_baseline_with_drift(db_session, project.id)
    cpm_axis = result["axes"][AXIS_COST_PER_MERGED_PR]
    assert cpm_axis["baseline"] == base_cpm
    assert cpm_axis["drift_pct"] == 50.0
    assert result["baseline_captured_at"] is not None


@pytest.mark.asyncio
async def test_read_baseline_with_drift_handles_missing_baseline(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await read_baseline_with_drift(db_session, project.id)
    assert result["baseline_captured_at"] is None
    assert result["last_snapshot_at"] is None
    for axis in ALL_AXES:
        entry = result["axes"][axis]
        assert entry["current"] is None
        assert entry["baseline"] is None
        assert entry["drift_pct"] is None


def test_last_run_due_first_run():
    now = _utcnow()
    assert _last_run_due(None, now) is True


def test_last_run_due_recent_run_skips():
    now = _utcnow()
    last = (now - timedelta(days=2)).isoformat()
    assert _last_run_due(last, now) is False


def test_last_run_due_old_run_fires():
    now = _utcnow()
    last = (now - timedelta(days=8)).isoformat()
    assert _last_run_due(last, now) is True


@pytest.mark.asyncio
async def test_maybe_write_weekly_snapshot_skips_when_recent(db_session):
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {"captured_at": _utcnow().isoformat()},
                "snapshots": [{"captured_at": _utcnow().isoformat()}],
                "last_run_at": _utcnow().isoformat(),
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    written = await maybe_write_weekly_snapshot(db_session, project.id)
    assert written is None


@pytest.mark.asyncio
async def test_maybe_write_weekly_snapshot_fires_when_due(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    written = await maybe_write_weekly_snapshot(db_session, project.id)
    assert written is not None
    assert AXIS_COST_PER_MERGED_PR in written


@pytest.mark.asyncio
async def test_efficiency_snapshot_service_only_starts_on_leader():
    """The snapshot service is wired only into the leader-elected path.

    Lifespan startup branches: when ``is_leader`` is False the function
    returns early after starting just per-instance services. The efficiency
    snapshot factory must live AFTER that early return — otherwise non-leader
    instances would also tick and produce duplicate snapshots.
    """
    import inspect

    from app import lifecycle

    source = inspect.getsource(lifecycle)
    # The non-leader path returns early; everything after the "owns the
    # background-leader lock" sentinel runs only on the leader.
    leader_marker = "owns the background-leader lock"
    assert leader_marker in source
    pre_leader, _, post_leader = source.partition(leader_marker)
    # Service factory and registration must live on the leader-only side.
    assert "_efficiency_snapshot_service" in post_leader
    assert "_efficiency_snapshot_service" not in pre_leader
    # Registered in task_factories so the watchdog restarts it on crash.
    assert '"efficiency_snapshot": _efficiency_snapshot_service' in post_leader


@pytest.mark.asyncio
async def test_efficiency_axes_endpoint_returns_shape(client: AsyncClient, db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/efficiency-axes")
    assert resp.status_code == 200
    data = resp.json()

    assert "axes" in data
    assert "baseline_captured_at" in data
    assert "last_snapshot_at" in data
    for axis in ALL_AXES:
        assert axis in data["axes"]
        entry = data["axes"][axis]
        assert set(entry.keys()) == {"current", "baseline", "drift_pct", "history"}
        assert isinstance(entry["history"], list)


@pytest.mark.asyncio
async def test_read_baseline_with_drift_history_reflects_snapshots(db_session):
    """history[] per axis must contain one entry per stored snapshot."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    result = await read_baseline_with_drift(db_session, project.id)
    for axis in ALL_AXES:
        hist = result["axes"][axis]["history"]
        assert len(hist) == 2, f"{axis}: expected 2 history entries, got {len(hist)}"
        for entry in hist:
            assert "captured_at" in entry
            assert "value" in entry


@pytest.mark.asyncio
async def test_efficiency_axes_endpoint_requires_membership(
    client: AsyncClient, db_session
):
    """A user without project membership gets a 404 (consistent with helper)."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Intentionally NO make_membership call.

    resp = await client.get(f"/api/admin/projects/{project.id}/efficiency-axes")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_axis_constants_used_consistently():
    """Writer + reader must agree on key vocabulary (fingerprint 691593a5c0a7)."""
    # Read uses the same constants as write — assert all five axis names match
    # the canonical strings the service ships with.
    assert AXIS_COST_PER_MERGED_PR == "cost_per_merged_pr"
    assert AXIS_VM_COLD_START_P50_MS == "vm_cold_start_p50_ms"
    assert AXIS_VM_REUSE_RATE == "vm_reuse_rate"
    assert AXIS_MEDIAN_API_CALLS_PER_TASK == "median_api_calls_per_task"
    assert AXIS_MEDIAN_REVIEW_ITERATIONS == "median_review_iterations"
