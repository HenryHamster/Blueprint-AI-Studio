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
    maybe_write_daily_snapshot,
    read_baseline_with_drift,
    write_snapshot,
)
from app.models import TaskStatus
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
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
    # Warm signal lives in context['_timing']['warm_start'] (the column is only
    # ever written False at cold-provision time); the cold task participates in
    # the denominator via its vm_provisioned_at.
    db_session.add(_make_efficient_task(project.id))
    db_session.add(
        _make_efficient_task(
            project.id,
            vm_cold_start_ms=None,
            api_call_summary={"llm_call_count": 80},
            context={
                "pr_merged": True,
                "review_loop": {"iteration": 4},
                "_timing": {"warm_start": True},
            },
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
async def test_vm_reuse_rate_reads_timing_warm_start(db_session):
    """Regression: reuse rate must read context['_timing']['warm_start'], the
    same source /pipeline-efficiency uses — NOT the vm_warm_start column, which
    is only ever written False at cold-provision time. Production reality is a
    column value of False with the warm signal carried in _timing.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # 2 warm: column False (production reality) but _timing.warm_start True.
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                vm_warm_start=False,
                vm_provisioned_at=now - timedelta(minutes=2),
                vm_stopped_at=now,
                context={"_timing": {"warm_start": True}},
            )
        )
    # 2 cold: _timing.warm_start False + a vm_provisioned_at (participates).
    for _ in range(2):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                vm_warm_start=False,
                vm_provisioned_at=now - timedelta(minutes=5),
                vm_stopped_at=now,
                context={"_timing": {"warm_start": False}},
            )
        )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    # 2 warm / (2 warm + 2 cold) = 0.5, despite every column value being False.
    assert axes[AXIS_VM_REUSE_RATE] == 0.5


@pytest.mark.asyncio
async def test_vm_reuse_rate_warm_without_column_or_provisioned(db_session):
    """Edge case: only context['_timing'].warm_start is set (no vm_warm_start
    column write, no vm_provisioned_at). The task still participates via its
    non-empty _timing dict and counts as warm.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_timing": {"warm_start": True}},
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert axes[AXIS_VM_REUSE_RATE] == 1.0


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
async def test_write_snapshot_migrates_stale_cold_start_baseline(db_session):
    """Existing baselines without _schema_version had cold-start captured from
    boot beacons (pre-fix). write_snapshot must clear that axis and migrate the
    baseline to the current schema head so the next snapshot re-seeds with
    honest data. A v0 baseline chains through every migration in one call
    (v0 -> v1 cold-start, v1 -> v2 review-iterations, v2 -> v3 cold-start),
    landing at v3.
    """
    stale_cold_start = 500  # artificially low — was a beacon time, not claim latency
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_VM_COLD_START_P50_MS: stale_cold_start,
                    AXIS_COST_PER_MERGED_PR: 0.01,
                    # No _schema_version — simulates a pre-fix baseline.
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add a task so the snapshot has real cold-start data (~15s).
    db_session.add(_make_efficient_task(project.id, vm_cold_start_ms=15000))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]

    # Old stale value must be gone; re-seeded from the new snapshot.
    assert baseline[AXIS_VM_COLD_START_P50_MS] != stale_cold_start
    assert baseline[AXIS_VM_COLD_START_P50_MS] == 15000.0
    # Migration marker applied — a v0 baseline chains to the current head (v3).
    assert baseline["_schema_version"] == 3
    # Other baseline axes must not be clobbered.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.01


@pytest.mark.asyncio
async def test_write_snapshot_reseeds_cleared_baseline_axis(db_session):
    """An axis explicitly cleared to None in the baseline must be re-seeded by
    the next write_snapshot call when a valid current value is available.
    """
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_VM_COLD_START_P50_MS: None,  # cleared axis
                    AXIS_COST_PER_MERGED_PR: 0.02,
                    "_schema_version": 1,  # already migrated, no re-clear
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id, vm_cold_start_ms=20000))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    # Cleared axis re-seeded from the fresh snapshot.
    assert baseline[AXIS_VM_COLD_START_P50_MS] == 20000.0
    # Non-cleared axes unchanged.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.02


@pytest.mark.asyncio
async def test_write_snapshot_migrates_cold_start_baseline_v2_to_v3(db_session):
    """Baselines at schema_version=2 stored vm_cold_start_p50_ms anchored at
    provision-confirm time (excludes GCE boot).  write_snapshot must clear that
    axis and bump to schema_version=3 so the next snapshot re-seeds with the
    corrected dispatch->agent-ready measurement."""
    stale_cold_start = 3000  # artificially low — provision-confirm only, no boot time
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_VM_COLD_START_P50_MS: stale_cold_start,
                    AXIS_COST_PER_MERGED_PR: 0.05,
                    "_schema_version": 2,
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id, vm_cold_start_ms=25000))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    # Stale provision-confirm-anchored value must be gone; re-seeded from new snapshot.
    assert baseline[AXIS_VM_COLD_START_P50_MS] != stale_cold_start
    assert baseline[AXIS_VM_COLD_START_P50_MS] == 25000.0
    # Schema migrated to v3.
    assert baseline["_schema_version"] == 3
    # Other baseline axes must not be clobbered.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.05


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
    last = (now - timedelta(hours=12)).isoformat()
    assert _last_run_due(last, now) is False


def test_last_run_due_old_run_fires():
    now = _utcnow()
    last = (now - timedelta(days=2)).isoformat()
    assert _last_run_due(last, now) is True


@pytest.mark.asyncio
async def test_maybe_write_daily_snapshot_skips_when_recent(db_session):
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

    written = await maybe_write_daily_snapshot(db_session, project.id)
    assert written is None


@pytest.mark.asyncio
async def test_maybe_write_daily_snapshot_fires_when_due(db_session):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    written = await maybe_write_daily_snapshot(db_session, project.id)
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


@pytest.mark.asyncio
async def test_cancelled_task_with_vm_cost_included_in_cost_per_merged_pr(db_session):
    """Regression test: cancelled tasks with vm_provisioned_at and vm_stopped_at
    should include their VM cost in cost_per_merged_pr aggregation.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # One merged task: 5 min on-demand
    db_session.add(
        _make_efficient_task(
            project.id,
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )
    )
    # One cancelled task with VM: 3 min spot. Should be included in cost but not in merged count.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.CANCELLED,
            vm_provisioned_at=now - timedelta(minutes=3),
            vm_stopped_at=now,
            vm_spot=True,
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # Total cost = 5*60*ON_DEMAND_RATE + 3*60*SPOT_RATE
    # Merged count = 1 (only the completed task with pr_merged)
    expected_cost = 5 * 60 * ON_DEMAND_RATE + 3 * 60 * SPOT_RATE
    expected_cpm = round(expected_cost / 1, 4)
    assert axes[AXIS_COST_PER_MERGED_PR] == expected_cpm


@pytest.mark.asyncio
async def test_review_skipped_tasks_count_as_zero_iterations(db_session):
    """Regression: a task that was auto-approved (pr_merged=True, no review_loop)
    must count as 0 review iterations instead of being excluded from the sample.
    Excluding auto-approved tasks inflates the median whenever the pipeline
    gets more efficient by skipping review, inverting the signal.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task that went through review: 4 iterations.
    db_session.add(
        _make_efficient_task(
            project.id, context={"pr_merged": True, "review_loop": {"iteration": 4}}
        )
    )
    # Task whose review was skipped (auto-approved small diff): no review_loop.
    db_session.add(_make_efficient_task(project.id, context={"pr_merged": True}))
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # Median of [4, 0] = 2.0. Without the fix this would be 4.0 (only the
    # reviewed task is counted, the auto-approved one is silently excluded).
    assert axes[AXIS_MEDIAN_REVIEW_ITERATIONS] == 2.0


@pytest.mark.asyncio
async def test_write_snapshot_migrates_review_iterations_baseline(db_session):
    """schema_version 1 baselines must have median_review_iterations cleared
    so the next snapshot re-seeds under the corrected (review-skipped) definition.
    """
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_MEDIAN_REVIEW_ITERATIONS: 4.0,  # stale — excluded auto-approved
                    AXIS_COST_PER_MERGED_PR: 0.01,
                    "_schema_version": 1,
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add a task so the snapshot has real review-iteration data.
    db_session.add(
        _make_efficient_task(
            project.id, context={"pr_merged": True, "review_loop": {"iteration": 2}}
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]

    # Stale value replaced by the corrected snapshot value.
    assert baseline[AXIS_MEDIAN_REVIEW_ITERATIONS] != 4.0
    assert baseline[AXIS_MEDIAN_REVIEW_ITERATIONS] == 2.0
    # v1 chains through v2 (review-iterations) and v3 (cold-start dispatch fix).
    assert baseline["_schema_version"] == 3
    # Other axes must not be clobbered.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.01


@pytest.mark.asyncio
async def test_write_snapshot_preserves_other_design_doc_keys(db_session):
    """write_snapshot must not clobber sibling keys written by concurrent
    background writers (e.g. playbook_learning, sr_blind_spots).

    Regression guard for the unlocked read-modify-write pattern: a writer
    that read design_doc before another writer committed would silently
    overwrite the other writer's keys on its own commit.  The fix routes
    write_snapshot through project_lock so the read is always post-lock.
    """
    sentinel = {"some_other_key": {"value": 42}}
    project = make_project(design_doc=dict(sentinel))
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    # The efficiency baseline key must be written.
    assert DESIGN_DOC_KEY in project.design_doc
    # Pre-existing sibling key must be intact.
    assert project.design_doc.get("some_other_key") == {"value": 42}
