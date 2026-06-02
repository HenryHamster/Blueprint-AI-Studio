"""Tests for backend/app/efficiency_snapshot.py and the admin endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.efficiency_snapshot import (
    ALL_AXES,
    AXIS_COST_PER_MERGED_PR,
    AXIS_LLM_COST_PER_MERGED_PR,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    DESIGN_DOC_KEY,
    LLM_TOKEN_PRICING,
    _compute_cold_start_breakdown_p50,
    _compute_llm_cost_pricing_coverage,
    _last_run_due,
    compute_current_axes,
    compute_llm_cost_breakdown,
    maybe_write_daily_snapshot,
    read_baseline_with_drift,
    write_snapshot,
)
from app.models import TaskStatus, bank_task_current_attempt
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

    # Two merged tasks, one on-demand and one e2-standard-4. 1 cold (12s), 1 warm (no cold sample).
    # Warm signal lives in context['_timing']['warm_start'], the source
    # _compute_vm_reuse_rate keys on; the cold task participates in the
    # denominator via its vm_provisioned_at. (vm_warm_start is False on cold
    # dispatch and True on warm claim, but the metric intentionally reads
    # _timing.warm_start — the agent-authoritative source.)
    db_session.add(_make_efficient_task(project.id))
    db_session.add(
        _make_efficient_task(
            project.id,
            vm_cold_start_ms=None,
            vm_machine_type="e2-standard-4",
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

    # 300s * (e2-standard-2 + e2-standard-4) / 2 merged.
    expected_cpm = round(300 * (ON_DEMAND_RATE + (0.134 / 3600)) / 2, 4)
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
    # Scalar axes are None when no tasks exist. Structural metadata keys
    # (vm_cold_start_breakdown, llm_cost_pricing_coverage) are exempt —
    # they return None / empty-dict rather than a scalar None.
    assert all(axes[k] is None for k in ALL_AXES)


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
    # Migration marker applied — a v0 baseline chains through all migrations to current head (v5).
    # v0 → v1 (cold-start), v1 → v2 (review-iterations), v2 → v3 (cold-start dispatch fix),
    # v3 → v4 (llm cost init), v4 → v5 (pricing table additions).
    assert baseline["_schema_version"] == 5
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
    # Schema migrated: v2 → v3 (tested here) plus v3 → v4, v4 → v5 (auto-chained).
    assert baseline["_schema_version"] == 5
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
    # v1 chains through v2 (review-iterations), v3 (cold-start dispatch fix),
    # v4 (llm cost init), and v5 (pricing table additions).
    assert baseline["_schema_version"] == 5
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


def _make_staged_task(project_id, dispatch_offset_s=0, stages=None, **overrides):
    """Create a Task with _cold_start_stages populated for breakdown tests."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    dispatch_at = now - timedelta(seconds=dispatch_offset_s + 60)
    defaults = dict(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"_cold_start_stages": stages or {}},
        vm_dispatched_at=dispatch_at,
    )
    defaults.update(overrides)
    return make_task(**defaults)


def test_compute_cold_start_breakdown_p50_basic():
    """Tasks with full stage timestamps produce correct per-stage p50 values."""
    import uuid

    project_id = uuid.uuid4()
    now = datetime(2026, 1, 1, 12, 0, 0)

    def _iso(dt):
        return dt.isoformat()

    dispatch_at = now
    # Stage durations (seconds): a=5, b=3, c=2, d=1, e=30, f=10
    stages = {
        "beacon_a_at": _iso(now + timedelta(seconds=5)),
        "beacon_b_at": _iso(now + timedelta(seconds=8)),
        "beacon_c_at": _iso(now + timedelta(seconds=10)),
        "beacon_d_at": _iso(now + timedelta(seconds=11)),
        "startup_complete_at": _iso(now + timedelta(seconds=41)),
        "claim_at": _iso(now + timedelta(seconds=51)),
    }
    t = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"_cold_start_stages": stages},
        vm_dispatched_at=dispatch_at,
    )

    result = _compute_cold_start_breakdown_p50([t])
    assert result is not None
    assert result["dispatch_to_creds_ms"] == 5000.0
    assert result["creds_to_env_ms"] == 3000.0
    assert result["env_to_payload_ms"] == 2000.0
    assert result["payload_to_systemctl_ms"] == 1000.0
    assert result["systemctl_to_startup_ms"] == 30000.0
    assert result["startup_to_claim_ms"] == 10000.0


def test_compute_cold_start_breakdown_p50_no_stage_data():
    """Tasks with no _cold_start_stages context return None."""
    import uuid

    project_id = uuid.uuid4()
    t = make_task(project_id=project_id, status=TaskStatus.COMPLETED, context={})
    assert _compute_cold_start_breakdown_p50([t]) is None


def test_compute_cold_start_breakdown_p50_partial_stages():
    """Tasks missing some beacons produce None for incomplete stage deltas."""
    import uuid

    project_id = uuid.uuid4()
    now = datetime(2026, 1, 1, 12, 0, 0)
    stages = {
        "beacon_a_at": now.isoformat(),
        "beacon_b_at": (now + timedelta(seconds=4)).isoformat(),
        # beacon_c_at..claim_at absent
    }
    t = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"_cold_start_stages": stages},
        vm_dispatched_at=now - timedelta(seconds=10),
    )
    result = _compute_cold_start_breakdown_p50([t])
    assert result is not None
    assert result["dispatch_to_creds_ms"] == 10000.0
    assert result["creds_to_env_ms"] == 4000.0
    assert result["env_to_payload_ms"] is None
    assert result["systemctl_to_startup_ms"] is None
    assert result["startup_to_claim_ms"] is None


def test_compute_cold_start_breakdown_p50_queue_wait_stage():
    """dispatch_to_slot_ms captures queue-wait time when provision_slot_acquired_at is present.

    Older tasks without this key produce None for the stage (not an error).
    """
    import uuid

    project_id = uuid.uuid4()
    now = datetime(2026, 1, 1, 12, 0, 0)

    # Task WITH slot-acquired timestamp: 8s queue wait.
    stages_with_slot = {
        "provision_slot_acquired_at": (now + timedelta(seconds=8)).isoformat(),
        "beacon_a_at": (now + timedelta(seconds=15)).isoformat(),
        "beacon_b_at": (now + timedelta(seconds=18)).isoformat(),
        "beacon_c_at": (now + timedelta(seconds=20)).isoformat(),
        "beacon_d_at": (now + timedelta(seconds=21)).isoformat(),
        "startup_complete_at": (now + timedelta(seconds=51)).isoformat(),
        "claim_at": (now + timedelta(seconds=61)).isoformat(),
    }
    t_with = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"_cold_start_stages": stages_with_slot},
        vm_dispatched_at=now,
    )
    result = _compute_cold_start_breakdown_p50([t_with])
    assert result is not None
    assert result["dispatch_to_slot_ms"] == 8000.0
    assert result["dispatch_to_creds_ms"] == 15000.0

    # Older task WITHOUT slot-acquired timestamp: dispatch_to_slot_ms is None.
    stages_without_slot = {
        "beacon_a_at": (now + timedelta(seconds=15)).isoformat(),
        "beacon_b_at": (now + timedelta(seconds=18)).isoformat(),
        "beacon_c_at": (now + timedelta(seconds=20)).isoformat(),
        "beacon_d_at": (now + timedelta(seconds=21)).isoformat(),
        "startup_complete_at": (now + timedelta(seconds=51)).isoformat(),
        "claim_at": (now + timedelta(seconds=61)).isoformat(),
    }
    t_old = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"_cold_start_stages": stages_without_slot},
        vm_dispatched_at=now,
    )
    result_old = _compute_cold_start_breakdown_p50([t_old])
    assert result_old is not None
    assert result_old["dispatch_to_slot_ms"] is None
    assert result_old["dispatch_to_creds_ms"] == 15000.0


@pytest.mark.asyncio
async def test_compute_current_axes_includes_breakdown(db_session):
    """compute_current_axes includes vm_cold_start_breakdown key."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime(2026, 1, 1, 12, 0, 0)
    stages = {
        "beacon_a_at": (now + timedelta(seconds=5)).isoformat(),
        "beacon_b_at": (now + timedelta(seconds=8)).isoformat(),
        "beacon_c_at": (now + timedelta(seconds=10)).isoformat(),
        "beacon_d_at": (now + timedelta(seconds=11)).isoformat(),
        "startup_complete_at": (now + timedelta(seconds=41)).isoformat(),
        "claim_at": (now + timedelta(seconds=51)).isoformat(),
    }
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_cold_start_stages": stages},
            vm_dispatched_at=now,
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert "vm_cold_start_breakdown" in axes
    breakdown = axes["vm_cold_start_breakdown"]
    assert breakdown is not None
    assert breakdown["dispatch_to_creds_ms"] == 5000.0
    assert breakdown["startup_to_claim_ms"] == 10000.0


def test_llm_cost_axis_in_all_axes():
    assert AXIS_LLM_COST_PER_MERGED_PR in ALL_AXES
    assert AXIS_LLM_COST_PER_MERGED_PR == "llm_cost_per_merged_pr"


@pytest.mark.asyncio
async def test_llm_token_pricing_known_model(db_session):
    """Task with known provider/model and token counts produces correct LLM cost per merged PR."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 10,
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    expected = round(1000 * input_rate + 500 * output_rate, 6)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == expected


@pytest.mark.asyncio
async def test_llm_token_pricing_unknown_model(db_session):
    """Task with missing provider/model contributes zero LLM cost and does not crash."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One merged task with no provider/model — LLM cost should be 0 / 1 merged = 0.0
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 10,
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
            },
            provider=None,
            model=None,
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == 0.0


@pytest.mark.asyncio
async def test_llm_token_pricing_no_token_data(db_session):
    """Task with no token data in api_call_summary contributes zero LLM cost."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={"llm_call_count": 10},
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == 0.0


@pytest.mark.asyncio
async def test_retry_banked_vm_and_llm_costs_count_across_attempts(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    first_started = now - timedelta(minutes=9)
    first_stopped = now - timedelta(minutes=5)
    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": True, "review_loop": {"iteration": 1}},
        vm_provisioned_at=first_started,
        vm_spot=True,
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    bank_task_current_attempt(task, stopped_at=first_stopped)
    await db_session.commit()
    await db_session.refresh(task)

    first_vm_cost = (first_stopped - first_started).total_seconds() * SPOT_RATE
    assert task.vm_runtime_cost_banked == pytest.approx(first_vm_cost)
    assert task.llm_input_tokens_banked == 1000
    assert task.llm_output_tokens_banked == 400
    assert task.llm_call_count_banked == 3
    assert task.vm_provisioned_at is None
    assert task.api_call_summary is None

    second_started = now - timedelta(minutes=3)
    task.status = TaskStatus.COMPLETED
    task.vm_provisioned_at = second_started
    task.vm_stopped_at = now
    task.vm_spot = False
    task.api_call_summary = {
        "llm_call_count": 2,
        "total_input_tokens": 250,
        "total_output_tokens": 150,
    }
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    expected_vm_cost = round(
        first_vm_cost + (now - second_started).total_seconds() * ON_DEMAND_RATE, 4
    )
    assert axes[AXIS_COST_PER_MERGED_PR] == pytest.approx(expected_vm_cost)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_llm_cost = round(
        (1250 * input_rate) + (550 * output_rate),
        6,
    )
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == pytest.approx(expected_llm_cost)

    refreshed = await db_session.get(type(task), task.id)
    breakdown = compute_llm_cost_breakdown([refreshed])
    assert breakdown["totals"]["total_input_tokens"] == 1250
    assert breakdown["totals"]["total_output_tokens"] == 550
    assert breakdown["by_model"][0]["llm_call_count"] == 5


@pytest.mark.asyncio
async def test_schema_version_v3_migrates_llm_axis(db_session):
    """Existing baseline at schema_version=3 gets llm_cost_per_merged_pr cleared
    and re-seeded, then bumped to schema_version=5 (via v4 intermediate).

    v3→v4: Clear stale llm_cost_per_merged_pr (before it was measured).
    v4→v5: Clear llm_cost_per_merged_pr again due to pricing table additions
            (gpt-5.3-codex-spark, gpt-5.2, Gemini 3.x preview models).
    """
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_LLM_COST_PER_MERGED_PR: 9999.0,  # stale / incorrect value
                    AXIS_COST_PER_MERGED_PR: 0.03,
                    "_schema_version": 3,
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 5,
                "total_input_tokens": 2000,
                "total_output_tokens": 1000,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]

    # Stale value must be gone; re-seeded from the new snapshot.
    assert baseline[AXIS_LLM_COST_PER_MERGED_PR] != 9999.0
    expected = round(2000 * input_rate + 1000 * output_rate, 6)
    assert baseline[AXIS_LLM_COST_PER_MERGED_PR] == expected
    # Schema bumped to v5 (v3 → v4 for llm cost, v4 → v5 for pricing table additions).
    assert baseline["_schema_version"] == 5
    # Other axes untouched.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.03


@pytest.mark.asyncio
async def test_llm_token_pricing_openai_model(db_session):
    """OpenAI task with token counts produces correct LLM cost per merged PR."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("openai", "gpt-5.4-mini")]
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 8,
                "total_input_tokens": 2000,
                "total_output_tokens": 800,
            },
            provider="openai",
            model="gpt-5.4-mini",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    expected = round(2000 * input_rate + 800 * output_rate, 6)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == expected


@pytest.mark.asyncio
async def test_llm_token_pricing_gemini_model(db_session):
    """Gemini task with explicitly-provided token counts produces correct LLM cost.

    In production Gemini tasks carry zero token counts today (agent telemetry gap —
    outputMode='text' in index.ts skips usage extraction). This test guards the
    pricing entry so it fires correctly once the agent-side gap is closed.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("gemini", "gemini-2.5-flash")]
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 5,
                "total_input_tokens": 3000,
                "total_output_tokens": 1200,
            },
            provider="gemini",
            model="gemini-2.5-flash",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    expected = round(3000 * input_rate + 1200 * output_rate, 6)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == expected


def test_llm_cost_pricing_coverage_shape():
    """Coverage dict: priced=1, unpriced=1, coverage_fraction=0.5, combo listed."""
    import uuid

    project_id = uuid.uuid4()

    # Priced task: Anthropic with tokens.
    priced = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Unpriced task: made-up provider+model with tokens.
    unpriced = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 200,
        },
        provider="fakeprovider",
        model="fake-model-x",
    )
    # Not-priceable task: no token counts.
    no_tokens = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"llm_call_count": 3},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = _compute_llm_cost_pricing_coverage([priced, unpriced, no_tokens])
    assert result["priced_tasks"] == 1
    assert result["unpriced_tasks"] == 1
    assert result["unattributed_tasks"] == 0
    assert result["coverage_fraction"] == 0.5
    assert "fakeprovider:fake-model-x" in result["unpriced_combos"]


def test_llm_cost_pricing_coverage_empty_window():
    """Empty window (no priceable tasks) yields coverage_fraction=None, empty combos."""
    import uuid

    project_id = uuid.uuid4()
    no_tokens = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"llm_call_count": 2},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    result = _compute_llm_cost_pricing_coverage([no_tokens])
    assert result["priced_tasks"] == 0
    assert result["unpriced_tasks"] == 0
    assert result["unattributed_tasks"] == 0
    assert result["coverage_fraction"] is None
    assert result["unpriced_combos"] == []


def test_llm_cost_pricing_coverage_unattributed_tasks():
    """Token-bearing tasks with null provider/model count as unattributed (not dropped)."""
    import uuid

    project_id = uuid.uuid4()

    priced = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Token-bearing task with no provider/model identity.
    unattributed = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 200,
            "total_output_tokens": 100,
        },
    )

    result = _compute_llm_cost_pricing_coverage([priced, unattributed])
    assert result["priced_tasks"] == 1
    assert result["unpriced_tasks"] == 0
    assert result["unattributed_tasks"] == 1
    # coverage_fraction denominator includes unattributed: 1 priced / 2 total
    assert result["coverage_fraction"] == 0.5
    assert result["unpriced_combos"] == []


def test_compute_llm_cost_breakdown_priced_and_unpriced():
    """Mixed priced/unpriced tasks across two providers and one unknown model."""
    import uuid

    project_id = uuid.uuid4()

    # Anthropic sonnet (priced): 1000 in @ 3e-6, 500 out @ 15e-6 = 0.0105
    anthropic_a = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Same model again to verify aggregation.
    anthropic_b = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "llm_call_count": 7,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # OpenAI gpt-5.4 (priced): 1000 in @ 5e-6, 200 out @ 25e-6 = 0.010
    openai_a = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 3,
        },
        provider="openai",
        model="gpt-5.4",
    )
    # Unknown model (unpriced): contributes to task counts but zero cost.
    unpriced = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
        },
        provider="fakeprovider",
        model="mystery-model",
    )
    # Skipped: no provider/model.
    no_meta = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 100,
            "total_output_tokens": 50,
        },
    )

    result = compute_llm_cost_breakdown(
        [anthropic_a, anthropic_b, openai_a, unpriced, no_meta]
    )

    by_provider = {entry["provider"]: entry for entry in result["by_provider"]}
    assert set(by_provider) == {"anthropic", "openai", "fakeprovider", "unattributed"}
    assert by_provider["anthropic"]["task_count"] == 2
    assert by_provider["anthropic"]["total_input_tokens"] == 3000
    assert by_provider["anthropic"]["total_output_tokens"] == 1500
    # 3000 * 3e-6 + 1500 * 15e-6 = 0.009 + 0.0225 = 0.0315
    assert by_provider["anthropic"]["total_cost_usd"] == round(0.0315, 6)
    assert by_provider["anthropic"]["llm_call_count"] == 12

    # 1000 * 5e-6 + 200 * 25e-6 = 0.005 + 0.005 = 0.010
    assert by_provider["openai"]["total_cost_usd"] == round(0.010, 6)
    assert by_provider["fakeprovider"]["total_cost_usd"] == 0.0
    # no_meta has tokens but no provider/model — routed to unattributed sentinel at zero cost
    assert by_provider["unattributed"]["task_count"] == 1
    assert by_provider["unattributed"]["total_cost_usd"] == 0.0

    by_model = {(e["provider"], e["model"]): e for e in result["by_model"]}
    assert by_model[("anthropic", "claude-sonnet-4-6")]["priced"] is True
    assert by_model[("fakeprovider", "mystery-model")]["priced"] is False
    assert ("unattributed", "unknown") in by_model

    # Priced shares sum to ~1.0 (within float tolerance) across by_provider.
    share_sum = sum(e["share_of_total_cost"] or 0.0 for e in result["by_provider"])
    assert abs(share_sum - 1.0) < 1e-6

    totals = result["totals"]
    # no_meta now counted: 4 original + 1 unattributed = 5
    assert totals["task_count"] == 5
    assert totals["priced_task_count"] == 3
    # unattributed tasks are unpriced (no pricing entry for sentinel key)
    assert totals["unpriced_task_count"] == 2
    assert totals["unpriced_coverage_fraction"] == round(3 / 5, 4)
    assert totals["total_cost_usd"] == round(0.0315 + 0.010, 6)

    assert "fakeprovider:mystery-model" in result["unpriced_combos"]
    assert "unattributed:unknown" in result["unpriced_combos"]


def test_compute_llm_cost_breakdown_empty():
    """No tasks -> empty buckets, zeroed totals, coverage_fraction=None."""
    result = compute_llm_cost_breakdown([])
    assert result["by_provider"] == []
    assert result["by_model"] == []
    assert result["unpriced_combos"] == []
    totals = result["totals"]
    assert totals["task_count"] == 0
    assert totals["priced_task_count"] == 0
    assert totals["unpriced_task_count"] == 0
    assert totals["total_cost_usd"] == 0.0
    assert totals["unpriced_coverage_fraction"] is None


def test_compute_llm_cost_breakdown_missing_provider_routes_to_unattributed():
    """Token-bearing tasks with missing provider/model route to unattributed sentinel bucket."""
    import uuid

    project_id = uuid.uuid4()
    missing_provider = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 100,
            "total_output_tokens": 50,
        },
        model="claude-sonnet-4-6",
    )
    missing_tokens = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"llm_call_count": 1},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    null_summary = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary=None,
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    valid = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 100,
            "total_output_tokens": 50,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = compute_llm_cost_breakdown(
        [missing_provider, missing_tokens, null_summary, valid]
    )
    # missing_provider has tokens: routes to unattributed (task_count=2 total)
    assert result["totals"]["task_count"] == 2
    assert (
        result["totals"]["unpriced_task_count"] == 1
    )  # unattributed sentinel is unpriced
    by_provider = {e["provider"]: e for e in result["by_provider"]}
    assert "unattributed" in by_provider
    assert by_provider["unattributed"]["total_cost_usd"] == 0.0
    assert "anthropic" in by_provider


@pytest.mark.asyncio
async def test_llm_cost_breakdown_endpoint_returns_rollup(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            api_call_summary={
                "total_input_tokens": 200,
                "total_output_tokens": 100,
            },
            provider="fakeprovider",
            model="mystery-model",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/llm-cost-breakdown")
    assert resp.status_code == 200
    data = resp.json()

    assert data["window_days"] == 7
    assert "generated_at" in data
    assert "by_provider" in data and "by_model" in data
    assert "totals" in data and "unpriced_combos" in data

    providers = {e["provider"] for e in data["by_provider"]}
    assert providers == {"anthropic", "fakeprovider"}
    assert data["totals"]["task_count"] == 2
    assert data["totals"]["priced_task_count"] == 1
    assert data["totals"]["unpriced_task_count"] == 1
    assert "fakeprovider:mystery-model" in data["unpriced_combos"]


@pytest.mark.asyncio
async def test_llm_cost_breakdown_endpoint_requires_membership(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # No membership.
    resp = await client.get(f"/api/admin/projects/{project.id}/llm-cost-breakdown")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_llm_cost_breakdown_endpoint_window_days_validation(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    # window_days=0 violates ge=1
    resp = await client.get(
        f"/api/admin/projects/{project.id}/llm-cost-breakdown?window_days=0"
    )
    assert resp.status_code == 422
    # window_days=91 violates le=90
    resp = await client.get(
        f"/api/admin/projects/{project.id}/llm-cost-breakdown?window_days=91"
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_read_baseline_with_drift_includes_reuse_funnel(db_session):
    """read_baseline_with_drift must include a reuse_funnel key with rolling counters."""
    import app.warm_backlog_reuse as wbr

    # The funnel counters are process-global rolling deques; clear them so
    # events recorded by earlier tests in the same shard don't bleed in.
    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await read_baseline_with_drift(db_session, project.id)
    assert "reuse_funnel" in result
    funnel = result["reuse_funnel"]
    assert "window_hours" in funnel
    assert "completed_vms_with_eligible_backlog" in funnel
    assert "teardown_defers_scheduled" in funnel
    assert "next_role_polls_during_defer" in funnel
    assert "warm_claims_succeeded" in funnel
    assert "claim_skipped_by_reason" in funnel
    assert "claim_skipped_total" in funnel
    # On a fresh instance all counters start at zero.
    assert funnel["completed_vms_with_eligible_backlog"] == 0
    assert funnel["warm_claims_succeeded"] == 0
