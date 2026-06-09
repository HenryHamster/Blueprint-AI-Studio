"""Tests for backend/app/efficiency_snapshot.py and the admin endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.efficiency_snapshot import (
    ALL_AXES,
    AXIS_CONSUMERS,
    AXIS_COST_PER_MERGED_PR,
    AXIS_LLM_COST_PER_MERGED_PR,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    DESIGN_DOC_KEY,
    FORMAT_COMPLIANCE_KEY,
    FORMAT_COMPLIANCE_RATE_KEY,
    LLM_TOKEN_PRICING,
    NESTED_METRIC_CONSUMERS,
    SELF_REPAIR_SUCCESS_RATE_KEY,
    _compute_cold_start_breakdown_p50,
    _compute_llm_cost_pricing_coverage,
    _last_run_due,
    compute_current_axes,
    compute_llm_cost_breakdown,
    compute_llm_cost_by_task_type,
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


def _make_merged_task(project_id, *, reason: str | None = None):
    context = {"pr_merged": True}
    if reason is not None:
        context["review_loop"] = {"unverified_merge_reason": reason}
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context=context,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )


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
async def test_cost_per_merged_pr_excludes_non_merge_unblocks(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    db_session.add(
        _make_efficient_task(
            project.id,
            context={
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            },
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    expected_cpm = round(2 * 300 * ON_DEMAND_RATE, 4)
    assert axes[AXIS_COST_PER_MERGED_PR] == expected_cpm


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
async def test_warm_start_origin_breakdown_per_origin_counts(db_session):
    """The per-origin breakdown isolates the pool-origin share of warm starts
    from chain-continuity and backlog-reuse, while leaving the aggregate
    vm_reuse_rate intact. Mixes pool/backlog/chain warm starts plus a cold task.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    def _warm(origin):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=2),
            vm_stopped_at=now,
            context={
                "_timing": {"warm_start": True, "warm_claim_origin": origin}
            },
        )

    # 2 pool, 1 backlog, 1 chain (next_role_endpoint), 1 legacy-unset chain.
    db_session.add(_warm("warm_pool"))
    db_session.add(_warm("warm_pool"))
    db_session.add(_warm("backlog_reuse"))
    db_session.add(_warm("next_role_endpoint"))
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=2),
            vm_stopped_at=now,
            context={"_timing": {"warm_start": True}},  # legacy-unset -> chain
        )
    )
    # 1 cold task: participates in vm_reuse_rate denominator but NOT the warm
    # breakdown (warm_start is False).
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            context={"_timing": {"warm_start": False}},
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    bd = axes["warm_start_origin_breakdown"]
    assert bd is not None
    assert bd["warm_pool"] == 2
    assert bd["backlog_reuse"] == 1
    assert bd["chain_continuity"] == 2
    assert bd["warm_total"] == 5
    assert bd["pool_origin_share"] == round(2 / 5, 4)
    # Aggregate reuse rate is intact and counts every warm origin: 5 warm / 6.
    assert axes[AXIS_VM_REUSE_RATE] == round(5 / 6, 4)


@pytest.mark.asyncio
async def test_warm_start_origin_breakdown_none_when_no_warm(db_session):
    """With only cold tasks the breakdown is None (no warm population to split)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            context={"_timing": {"warm_start": False}},
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert axes["warm_start_origin_breakdown"] is None


@pytest.mark.asyncio
async def test_read_baseline_surfaces_warm_start_origin_breakdown(db_session):
    """read_baseline_with_drift surfaces the latest snapshot's per-origin
    breakdown next to the pool idle cost and cost-per-merged-PR figures."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=2),
            vm_stopped_at=now,
            context={
                "_timing": {"warm_start": True, "warm_claim_origin": "warm_pool"}
            },
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    drift = await read_baseline_with_drift(db_session, project.id)
    bd = drift["warm_start_origin_breakdown"]
    assert bd is not None
    assert bd["warm_pool"] == 1
    assert bd["pool_origin_share"] == 1.0


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
async def test_compute_llm_cost_breakdown_credits_each_attempt_model(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        vm_provisioned_at=now - timedelta(minutes=9),
        vm_spot=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))
    await db_session.commit()
    await db_session.refresh(task)

    assert task.attempt_history[0]["provider"] == "anthropic"
    assert task.attempt_history[0]["model"] == "claude-sonnet-4-6"

    task.status = TaskStatus.COMPLETED
    task.context = {"pr_merged": True}
    task.provider = "openai"
    task.model = "gpt-5.4"
    task.vm_provisioned_at = now - timedelta(minutes=3)
    task.vm_stopped_at = now
    task.vm_spot = False
    task.api_call_summary = {
        "llm_call_count": 2,
        "total_input_tokens": 250,
        "total_output_tokens": 150,
    }
    await db_session.commit()

    refreshed = await db_session.get(type(task), task.id)
    breakdown = compute_llm_cost_breakdown([refreshed])
    by_model = {(e["provider"], e["model"]): e for e in breakdown["by_model"]}
    anthropic_row = by_model[("anthropic", "claude-sonnet-4-6")]
    openai_row = by_model[("openai", "gpt-5.4")]
    assert anthropic_row["total_input_tokens"] == 1000
    assert anthropic_row["total_output_tokens"] == 400
    assert openai_row["total_input_tokens"] == 250
    assert openai_row["total_output_tokens"] == 150

    # One MERGED task spanning two model buckets credits the merge once per
    # distinct bucket — never per attempt-row — and exactly once to the total
    # (no double-credit across buckets).
    assert anthropic_row["merged_count"] == 1
    assert openai_row["merged_count"] == 1
    assert anthropic_row["merge_rate"] == 1.0
    assert openai_row["merge_rate"] == 1.0
    assert anthropic_row["cost_per_merge"] == round(anthropic_row["total_cost_usd"] / 1, 6)
    assert openai_row["cost_per_merge"] == round(openai_row["total_cost_usd"] / 1, 6)

    totals = breakdown["totals"]
    assert totals["task_count"] == 2  # two attempt-rows
    assert totals["merged_count"] == 1  # one merged TASK, not double-credited
    assert totals["merge_rate"] == round(1 / 2, 4)


@pytest.mark.asyncio
async def test_compute_llm_cost_breakdown_merge_counts_multi_attempt_task_once(
    db_session,
):
    """A multi-attempt MERGED task in a single model bucket credits merged_count
    once (not once per attempt), so merge_rate and cost_per_merge divide by the
    merge count — not the attempt count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        vm_provisioned_at=now - timedelta(minutes=9),
        vm_spot=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Bank a first attempt, then run a second attempt on the SAME model.
    bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))
    await db_session.commit()
    await db_session.refresh(task)

    task.status = TaskStatus.COMPLETED
    task.context = {"pr_merged": True}
    task.vm_provisioned_at = now - timedelta(minutes=3)
    task.vm_stopped_at = now
    task.vm_spot = False
    task.api_call_summary = {
        "llm_call_count": 2,
        "total_input_tokens": 250,
        "total_output_tokens": 150,
    }
    await db_session.commit()

    refreshed = await db_session.get(type(task), task.id)
    breakdown = compute_llm_cost_breakdown([refreshed])
    row = {(e["provider"], e["model"]): e for e in breakdown["by_model"]}[
        ("anthropic", "claude-sonnet-4-6")
    ]
    # Two attempt-rows in one bucket, but a single merged task.
    assert row["task_count"] == 2
    assert row["merged_count"] == 1
    assert row["merge_rate"] == round(1 / 2, 4)
    # cost_per_merge divides total bucket cost by the merge COUNT (1), not the
    # attempt count (2): the per-attempt accumulation would halve this figure.
    assert row["cost_per_merge"] == round(row["total_cost_usd"] / 1, 6)

    totals = breakdown["totals"]
    assert totals["merged_count"] == 1
    assert totals["merge_rate"] == round(1 / 2, 4)


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
    # OpenAI gpt-5.4 (priced): 1000 in @ 2.5e-6, 200 out @ 15e-6 = 0.0055
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

    # 1000 * 2.5e-6 + 200 * 15e-6 = 0.0025 + 0.003 = 0.0055
    assert by_provider["openai"]["total_cost_usd"] == round(0.0055, 6)
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
    assert totals["total_cost_usd"] == round(0.0315 + 0.0055, 6)

    assert "fakeprovider:mystery-model" in result["unpriced_combos"]
    assert "unattributed:unknown" in result["unpriced_combos"]

    # None of these tasks carry the landed signal, so every bucket reads as an
    # explicit 0-of-N merged with no confident cost-per-merge (no divide-by-zero).
    assert by_provider["anthropic"]["merged_count"] == 0
    assert by_provider["anthropic"]["merge_rate"] == 0.0
    assert by_provider["anthropic"]["cost_per_merge"] is None
    assert by_model[("anthropic", "claude-sonnet-4-6")]["merged_count"] == 0
    assert by_model[("anthropic", "claude-sonnet-4-6")]["cost_per_merge"] is None
    assert totals["merged_count"] == 0
    assert totals["merge_rate"] == 0.0


def test_compute_llm_cost_breakdown_approximate_priced():
    """Approximate-priced combos (proxy rate, not exact published pricing) are marked distinctly from unpriced."""
    import uuid

    project_id = uuid.uuid4()

    # gpt-5.3-codex-spark: priced at 3e-6 / 12e-6 (closest GA equivalent) — NO exact published rate.
    # 1000 in @ 3e-6, 500 out @ 12e-6 = 0.003 + 0.006 = 0.009
    approximate_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 5,
        },
        provider="openai",
        model="gpt-5.3-codex-spark",
    )

    # Exact-priced for comparison (gpt-5.4 has published rate).
    exact_task = make_task(
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

    result = compute_llm_cost_breakdown([approximate_task, exact_task])

    by_model = {(e["provider"], e["model"]): e for e in result["by_model"]}
    # Approximate-priced combo is marked as both priced AND approximate.
    approx_row = by_model[("openai", "gpt-5.3-codex-spark")]
    assert approx_row["priced"] is True
    assert approx_row["approximate_priced"] is True
    # Exact-priced combo is priced but NOT approximate.
    exact_row = by_model[("openai", "gpt-5.4")]
    assert exact_row["priced"] is True
    assert exact_row["approximate_priced"] is False

    # Approximate combo appears in the caveated list, NOT in unpriced_combos.
    assert "openai:gpt-5.3-codex-spark" in result["approximate_priced_combos"]
    assert "openai:gpt-5.3-codex-spark" not in result["unpriced_combos"]
    assert result["unpriced_combos"] == []


def test_compute_llm_cost_breakdown_merge_rate_and_cost_per_merge():
    """Priced bucket with merges yields cost-per-merge; unpriced bucket suppresses it."""
    import uuid

    project_id = uuid.uuid4()

    # Two anthropic sonnet tasks, ONE merged. cost = 0.0105 each, total 0.021.
    merged = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    not_merged = make_task(
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
    # Unpriced but merged: cost is understated, so cost_per_merge must stay None.
    unpriced_merged = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
        },
        provider="fakeprovider",
        model="mystery-model",
    )

    result = compute_llm_cost_breakdown([merged, not_merged, unpriced_merged])
    by_model = {(e["provider"], e["model"]): e for e in result["by_model"]}
    by_provider = {e["provider"]: e for e in result["by_provider"]}

    sonnet = by_model[("anthropic", "claude-sonnet-4-6")]
    assert sonnet["merged_count"] == 1
    assert sonnet["verified_merged_count"] == 1
    assert sonnet["merge_rate"] == round(1 / 2, 4)
    assert sonnet["verified_merge_rate"] == round(1 / 2, 4)
    # priced bucket: cost_per_merge = total_cost / merged = 0.021 / 1
    assert sonnet["cost_per_merge"] == round(sonnet["total_cost_usd"] / 1, 6)

    fake = by_model[("fakeprovider", "mystery-model")]
    assert fake["merged_count"] == 1
    assert fake["merge_rate"] == 1.0
    # unpriced -> understated cost -> no confident cost-per-merge
    assert fake["cost_per_merge"] is None
    assert fake["priced"] is False

    # Provider with a single unpriced model is itself flagged unpriced.
    assert by_provider["fakeprovider"]["priced"] is False
    assert by_provider["fakeprovider"]["cost_per_merge"] is None
    assert by_provider["anthropic"]["priced"] is True
    assert by_provider["anthropic"]["cost_per_merge"] == round(
        by_provider["anthropic"]["total_cost_usd"] / 1, 6
    )

    totals = result["totals"]
    assert totals["merged_count"] == 2
    assert totals["verified_merged_count"] == 2
    assert totals["merge_rate"] == round(2 / 3, 4)
    assert totals["verified_merge_rate"] == round(2 / 3, 4)


def test_compute_llm_cost_breakdown_verified_excludes_degraded_merges():
    """Degraded fall-open merges still count as landed, but not verified."""
    import uuid

    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS

    project_id = uuid.uuid4()
    degraded_reason = next(iter(DEGRADED_UNVERIFIED_REASONS))

    verified = _make_merged_task(project_id)
    degraded = _make_merged_task(project_id, reason=degraded_reason)

    result = compute_llm_cost_breakdown([verified, degraded])
    by_model = {(e["provider"], e["model"]): e for e in result["by_model"]}
    by_provider = {e["provider"]: e for e in result["by_provider"]}

    model_row = by_model[("anthropic", "claude-sonnet-4-6")]
    provider_row = by_provider["anthropic"]

    assert model_row["merged_count"] == 2
    assert model_row["verified_merged_count"] == 1
    assert model_row["merge_rate"] == 1.0
    assert model_row["verified_merge_rate"] == 0.5
    assert model_row["cost_per_merge"] == round(model_row["total_cost_usd"] / 1, 6)

    assert provider_row["merged_count"] == 2
    assert provider_row["verified_merged_count"] == 1
    assert provider_row["merge_rate"] == 1.0
    assert provider_row["verified_merge_rate"] == 0.5
    assert provider_row["cost_per_merge"] == round(
        provider_row["total_cost_usd"] / 1, 6
    )

    totals = result["totals"]
    assert totals["merged_count"] == 2
    assert totals["verified_merged_count"] == 1
    assert totals["merge_rate"] == 1.0
    assert totals["verified_merge_rate"] == 0.5
    assert totals["total_cost_usd"] == model_row["total_cost_usd"]


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
    assert totals["merged_count"] == 0
    assert totals["merge_rate"] is None
    # No-fallback case: caveat stays at zero so the panel renders uncluttered.
    assert totals["fallback_task_count"] == 0
    assert totals["fallback_cost_usd"] == 0.0


def test_compute_llm_cost_breakdown_fallback_caveat():
    """Tasks stamped with routing_fallback / model_fallback are counted and
    their spend summed; clean tasks never inflate the fallback caveat."""
    import uuid

    project_id = uuid.uuid4()

    # Intended-routing task (no marker): 1000 in @ 3e-6, 500 out @ 15e-6 = 0.0105
    clean = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 1000, "total_output_tokens": 500},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Cross-provider fallback: resolved onto sonnet but intended anthropic→openai.
    cross = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={
            "routing_fallback": {
                "intended_provider": "openai",
                "effective_provider": "anthropic",
                "reason": "cross_provider",
            }
        },
        api_call_summary={"total_input_tokens": 1000, "total_output_tokens": 500},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Same-provider preview→GA downgrade marker also counts as a fallback.
    downgrade = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"model_fallback": {"requested": "x-preview", "effective": "x-ga"}},
        api_call_summary={"total_input_tokens": 2000, "total_output_tokens": 1000},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = compute_llm_cost_breakdown([clean, cross, downgrade])
    totals = result["totals"]
    assert totals["fallback_task_count"] == 2
    # cross = 0.0105, downgrade = 2000*3e-6 + 1000*15e-6 = 0.021 -> 0.0315
    assert totals["fallback_cost_usd"] == round(0.0105 + 0.021, 6)

    clean_only = compute_llm_cost_breakdown([clean])
    assert clean_only["totals"]["fallback_task_count"] == 0
    assert clean_only["totals"]["fallback_cost_usd"] == 0.0


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
    assert data["totals"]["merged_count"] == 0
    assert data["totals"]["verified_merged_count"] == 0
    assert data["totals"]["merge_rate"] == 0.0
    assert data["totals"]["verified_merge_rate"] == 0.0
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


@pytest.mark.asyncio
async def test_snapshot_persists_reuse_dropout_and_surfaces_trend(db_session):
    """write_snapshot must fold the dominant reuse drop-out into the durable
    snapshot, and read_baseline_with_drift must surface the latest ranking plus
    a day-over-day trend that survives the in-memory funnel reset."""
    import app.warm_backlog_reuse as wbr

    # Clear the process-global rolling funnel deques, then seed a clear leader:
    # 5 file-conflict scan skips dominate the non-converting population.
    for dq in (
        wbr._scan_skipped_depends_on_events,
        wbr._scan_skipped_pinned_vm_events,
        wbr._scan_skipped_file_conflict_events,
        wbr._scan_saturated_events,
        wbr._grace_idle_release_events,
        wbr._grace_ceiling_release_events,
        wbr._claim_skipped_events,
    ):
        dq.clear()
    for _ in range(5):
        wbr.record_scan_skipped_file_conflict()
    wbr.record_grace_idle_release()

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    snapshot = await write_snapshot(db_session, project.id)
    # The drop-out ranking is persisted in the snapshot body (survives restart).
    assert snapshot["reuse_dropout"]["dominant_dropout"]["reason"] == "scan_file_conflict"
    assert snapshot["reuse_dropout"]["dominant_dropout"]["count"] == 5

    # Simulate a deploy/restart wiping the in-memory funnel counters.
    for dq in (
        wbr._scan_skipped_file_conflict_events,
        wbr._grace_idle_release_events,
    ):
        dq.clear()

    result = await read_baseline_with_drift(db_session, project.id)
    # Latest persisted ranking is still surfaced despite the wiped live funnel.
    assert result["reuse_dropout"]["dominant_dropout"]["reason"] == "scan_file_conflict"
    trend = result["reuse_dropout_trend"]
    assert len(trend) == 1
    assert trend[0]["dominant_reason"] == "scan_file_conflict"
    assert trend[0]["dominant_share"] == result["reuse_dropout"]["dominant_dropout"]["share"]
    assert trend[0]["dropout_total"] == 6


@pytest.mark.asyncio
async def test_compute_current_axes_persists_pool_dispatch_funnel(db_session):
    """compute_current_axes folds the live pool dispatch funnel into the snapshot,
    carrying lease hits, cold dispatches, the lease-miss-by-reason breakdown, and
    the dominant-miss call-out (named per the >=2 / >=50% / strictly-outranks gate)."""
    import app.warm_backlog_reuse as wbr
    from app.efficiency_snapshot import compute_current_axes

    # Fresh process-global pool funnel deques, then seed a clear dominant miss:
    # 3 no_compatible_member misses dominate over 1 no_idle_member miss, plus a
    # hit and a cold dispatch so the hit-rate aggregates are exercised too.
    wbr._pool_lease_hit_events.clear()
    wbr._pool_cold_dispatch_events.clear()
    wbr._pool_lease_miss_events.clear()
    wbr.record_pool_lease_hit("acct-a")
    wbr.record_pool_cold_dispatch("acct-a")
    for _ in range(3):
        wbr.record_pool_lease_miss("no_compatible_member", "acct-a")
    wbr.record_pool_lease_miss("no_idle_member", "acct-b")

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    axes = await compute_current_axes(db_session, project.id)
    funnel = axes["pool_dispatch_funnel"]
    assert funnel["pool_lease_hits"] == 1
    assert funnel["cold_dispatches"] == 1
    assert funnel["pool_hit_rate"] == 0.5
    assert funnel["lease_miss_by_reason"]["no_compatible_member"] == 3
    assert funnel["lease_miss_by_reason"]["no_idle_member"] == 1
    assert funnel["lease_miss_total"] == 4
    # Dominant miss: 3/4 = 0.75 >= 0.5, >=2 events, strictly outranks the runner-up.
    assert funnel["dominant_miss"]["reason"] == "no_compatible_member"
    assert funnel["dominant_miss"]["count"] == 3
    assert funnel["dominant_miss"]["share"] == 0.75
    # Per-account breakdown rides along for the controlled-enable view.
    assert funnel["by_account"]["acct-a"]["pool_lease_hits"] == 1


@pytest.mark.asyncio
async def test_read_baseline_returns_pool_dispatch_funnel_trend(db_session):
    """write_snapshot persists the pool dispatch funnel, and
    read_baseline_with_drift surfaces the latest block plus a day-over-day trend
    that survives the in-memory pool funnel reset."""
    import app.warm_backlog_reuse as wbr

    wbr._pool_lease_hit_events.clear()
    wbr._pool_cold_dispatch_events.clear()
    wbr._pool_lease_miss_events.clear()
    wbr.record_pool_lease_hit("acct-a")
    for _ in range(2):
        wbr.record_pool_lease_miss("no_idle_member", "acct-a")

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    snapshot = await write_snapshot(db_session, project.id)
    assert snapshot["pool_dispatch_funnel"]["dominant_miss"]["reason"] == "no_idle_member"
    assert snapshot["pool_dispatch_funnel"]["pool_lease_hits"] == 1

    # Simulate a deploy/restart wiping the in-memory pool funnel counters.
    wbr._pool_lease_hit_events.clear()
    wbr._pool_lease_miss_events.clear()

    result = await read_baseline_with_drift(db_session, project.id)
    # Latest persisted funnel is still surfaced despite the wiped live counters.
    assert result["pool_dispatch_funnel"]["dominant_miss"]["reason"] == "no_idle_member"
    trend = result["pool_dispatch_funnel_trend"]
    assert len(trend) == 1
    assert trend[0]["pool_lease_hits"] == 1
    assert trend[0]["lease_miss_total"] == 2
    assert trend[0]["dominant_miss_reason"] == "no_idle_member"


def test_compute_dominant_pool_miss_no_leader_on_even_split():
    """An even split between two miss reasons names no dominant leader — the same
    strictly-outranks discipline the reuse-dropout ranking uses."""
    import app.warm_backlog_reuse as wbr

    wbr._pool_lease_hit_events.clear()
    wbr._pool_cold_dispatch_events.clear()
    wbr._pool_lease_miss_events.clear()
    for _ in range(2):
        wbr.record_pool_lease_miss("no_idle_member")
    for _ in range(2):
        wbr.record_pool_lease_miss("no_compatible_member")

    result = wbr.compute_dominant_pool_miss()
    assert result["lease_miss_total"] == 4
    # 2 vs 2: neither strictly outranks the other => no dominant miss named.
    assert result["dominant_miss"] is None


def test_compute_pool_cost_amortization_no_hits():
    """No warm-start hit yet => idle spend is not amortized at all."""
    from app.efficiency_snapshot import compute_pool_cost_amortization

    funnel = {
        "window_hours": 24,
        "pool_lease_hits": 0,
        "pool_hit_rate": 0.0,
        "total_dispatches": 30,
    }
    result = compute_pool_cost_amortization(5.0, funnel)
    assert result["pool_idle_cost_usd"] == 5.0
    assert result["pool_lease_hits"] == 0
    assert result["total_dispatches"] == 30
    # No hits => cost-per-hit is undefined (not amortized).
    assert result["cost_per_pool_hit_usd"] is None


def test_compute_pool_cost_amortization_with_hits():
    """Idle spend divided by warm-start hits gives the per-hit amortized cost."""
    from app.efficiency_snapshot import compute_pool_cost_amortization

    funnel = {
        "window_hours": 24,
        "pool_lease_hits": 4,
        "pool_hit_rate": 0.4,
        "total_dispatches": 10,
    }
    result = compute_pool_cost_amortization(6.0, funnel)
    assert result["cost_per_pool_hit_usd"] == 1.5
    assert result["pool_hit_rate"] == 0.4
    assert result["window_hours"] == 24


def test_compute_pool_cost_amortization_handles_none_idle():
    """A None/zero idle cost is coerced to 0.0 without raising."""
    from app.efficiency_snapshot import compute_pool_cost_amortization

    result = compute_pool_cost_amortization(None, {"pool_lease_hits": 0})
    assert result["pool_idle_cost_usd"] == 0.0
    assert result["cost_per_pool_hit_usd"] is None
    assert result["total_dispatches"] == 0


@pytest.mark.asyncio
async def test_read_baseline_with_drift_includes_pool_cost_amortization(db_session):
    """read_baseline_with_drift surfaces the pool cost-amortization companion."""
    import app.warm_backlog_reuse as wbr

    wbr._pool_lease_hit_events.clear()
    wbr._pool_cold_dispatch_events.clear()
    wbr._pool_lease_miss_events.clear()

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await read_baseline_with_drift(db_session, project.id)
    assert "pool_cost_amortization" in result
    amort = result["pool_cost_amortization"]
    assert "pool_idle_cost_usd" in amort
    assert "pool_lease_hits" in amort
    assert "cost_per_pool_hit_usd" in amort
    # Fresh instance: no idle cost recorded and no hits => not amortized.
    assert amort["pool_lease_hits"] == 0
    assert amort["cost_per_pool_hit_usd"] is None


# ---------------------------------------------------------------------------
# Per-project warm-pool PAYOFF VERDICT
# ---------------------------------------------------------------------------

from app.efficiency_snapshot import (  # noqa: E402
    POOL_PAYOFF_COST_REGRESSION,
    POOL_PAYOFF_INSUFFICIENT_DATA,
    POOL_PAYOFF_MIN_POOL_DISPATCHES,
    POOL_PAYOFF_NO_LIFT,
    POOL_PAYOFF_PROVEN,
    compute_pool_payoff_verdict,
)


def _cost_axis(current, baseline):
    drift = None
    if (
        isinstance(current, (int, float))
        and isinstance(baseline, (int, float))
        and baseline != 0
    ):
        drift = round(((current - baseline) / baseline) * 100.0, 2)
    return {"current": current, "baseline": baseline, "drift_pct": drift}


def test_pool_payoff_verdict_proven():
    """Serving warm starts above the floor AND cost within tolerance => PROVEN."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 8, "pool_origin_share": 0.5},
        _cost_axis(10.0, 10.2),  # cost slightly BELOW baseline => no regression
        {"pool_hit_rate": 0.4, "total_dispatches": 50},
    )
    assert verdict["state"] == POOL_PAYOFF_PROVEN
    assert verdict["supporting"]["pool_origin_share"] == 0.5
    assert verdict["supporting"]["pool_hit_rate"] == 0.4
    assert "widening" in verdict["recommended_action"].lower()


def test_pool_payoff_verdict_cost_regression():
    """Idle-inclusive cost materially above baseline => COST_REGRESSION, even
    when the pool is serving warm starts."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 8, "pool_origin_share": 0.5},
        _cost_axis(12.0, 10.0),  # +20% drift, well over the 5% tolerance
        {"pool_hit_rate": 0.4, "total_dispatches": 50},
    )
    assert verdict["state"] == POOL_PAYOFF_COST_REGRESSION
    assert verdict["supporting"]["cost_drift_pct"] == 20.0
    assert "disable" in verdict["recommended_action"].lower()


def test_pool_payoff_verdict_no_lift():
    """Cost not regressed but the pool is barely serving (hit rate AND
    pool-origin share below the floor) => NO_LIFT."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 1, "pool_origin_share": 0.02},
        _cost_axis(10.1, 10.0),  # +1% drift, within tolerance
        {"pool_hit_rate": 0.01, "total_dispatches": 50},
    )
    assert verdict["state"] == POOL_PAYOFF_NO_LIFT
    assert "tune" in verdict["recommended_action"].lower()


def test_pool_payoff_verdict_insufficient_data_no_baseline():
    """No cost baseline to compare against => INSUFFICIENT_DATA."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 8, "pool_origin_share": 0.5},
        _cost_axis(10.0, None),
        {"pool_hit_rate": 0.4, "total_dispatches": 50},
    )
    assert verdict["state"] == POOL_PAYOFF_INSUFFICIENT_DATA


def test_pool_payoff_verdict_insufficient_data_too_few_dispatches():
    """Fewer pool dispatches than the minimum sample size => INSUFFICIENT_DATA,
    even with a cost baseline present."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 8, "pool_origin_share": 0.5},
        _cost_axis(10.0, 10.0),
        {"pool_hit_rate": 0.4, "total_dispatches": POOL_PAYOFF_MIN_POOL_DISPATCHES - 1},
    )
    assert verdict["state"] == POOL_PAYOFF_INSUFFICIENT_DATA


def test_pool_payoff_verdict_serving_via_origin_share_only():
    """Hit rate below floor but pool-origin share above it still counts as
    serving (the OR), so with cost in tolerance the verdict is PROVEN."""
    verdict = compute_pool_payoff_verdict(
        {"warm_pool": 6, "pool_origin_share": 0.3},
        _cost_axis(10.0, 10.0),
        {"pool_hit_rate": 0.05, "total_dispatches": 50},
    )
    assert verdict["state"] == POOL_PAYOFF_PROVEN


def test_pool_payoff_verdict_handles_none_inputs():
    """None breakdown/funnel/axis must not raise — classify INSUFFICIENT_DATA."""
    verdict = compute_pool_payoff_verdict(None, None, None)
    assert verdict["state"] == POOL_PAYOFF_INSUFFICIENT_DATA
    assert verdict["supporting"]["pool_origin_share"] is None


@pytest.mark.asyncio
async def test_read_baseline_with_drift_includes_pool_payoff_verdict(db_session):
    """read_baseline_with_drift surfaces a pool_payoff_verdict block of the
    expected shape; a fresh project with no baseline/dispatches => INSUFFICIENT_DATA."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await read_baseline_with_drift(db_session, project.id)
    assert "pool_payoff_verdict" in result
    verdict = result["pool_payoff_verdict"]
    assert verdict["state"] == POOL_PAYOFF_INSUFFICIENT_DATA
    assert "supporting" in verdict
    assert "recommended_action" in verdict


# ---------------------------------------------------------------------------
# Per-project warm-pool payoff TRANSITION ALERT
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from sqlmodel import select as _sql_select  # noqa: E402

from app.efficiency_snapshot import (  # noqa: E402
    LAST_ALERTED_PAYOFF_KEY,
    POOL_PAYOFF_PROVEN_MIN_WARM_STARTS,
    maybe_alert_pool_payoff_transition,
)
from app.models import Alert, AlertSeverity  # noqa: E402
from app.shared_state import runtime_config as _runtime_config  # noqa: E402


def _payoff_design_doc(
    *,
    incl_current,
    cost_baseline,
    warm_pool,
    pool_origin_share,
    hit_rate,
    total_dispatches,
):
    """Build a design_doc whose latest snapshot drives a chosen payoff verdict.

    read_baseline_with_drift reads the idle-inclusive cost drift off
    ``cost_per_merged_pr_incl_pool_idle`` (current) vs the immutable
    ``cost_per_merged_pr`` baseline, the lift off the persisted
    ``warm_start_origin_breakdown`` + ``pool_dispatch_funnel``.
    """
    snap = {
        "captured_at": _utcnow().isoformat(),
        "cost_per_merged_pr_incl_pool_idle": incl_current,
        "warm_start_origin_breakdown": {
            "warm_pool": warm_pool,
            "pool_origin_share": pool_origin_share,
        },
        "pool_dispatch_funnel": {
            "pool_hit_rate": hit_rate,
            "total_dispatches": total_dispatches,
        },
    }
    return {
        DESIGN_DOC_KEY: {
            "baseline": {AXIS_COST_PER_MERGED_PR: cost_baseline},
            "snapshots": [snap],
        }
    }


async def _payoff_alerts(db_session, project_id):
    stmt = _sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("Warm-pool payoff"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_payoff_transition_cost_regression_fires_warning(db_session, mocker):
    """A transition to COST_REGRESSION fires one WARNING carrying the numbers."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=12.0,
            cost_baseline=10.0,  # +20% idle-inclusive drift, over the 5% tolerance
            warm_pool=8,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired == POOL_PAYOFF_COST_REGRESSION

    alerts = await _payoff_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "COST_REGRESSION" in alerts[0].title
    # Supporting numbers must be embedded so the operator can act from the alert.
    assert "20.00%" in alerts[0].message  # idle-inclusive cost drift
    assert "0.4" in alerts[0].message  # pool hit rate
    assert "8 warm starts" in alerts[0].message
    bcast.assert_awaited()

    # The marker advanced so a repeat tick is deduped (see next test's path).
    await db_session.refresh(project)
    assert (
        project.design_doc[DESIGN_DOC_KEY][LAST_ALERTED_PAYOFF_KEY]
        == POOL_PAYOFF_COST_REGRESSION
    )


@pytest.mark.asyncio
async def test_payoff_transition_dedups_same_verdict(db_session, mocker):
    """A second tick at the same verdict does not fire a second alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=12.0,
            cost_baseline=10.0,
            warm_pool=8,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await maybe_alert_pool_payoff_transition(db_session, project.id)
    second = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert first == POOL_PAYOFF_COST_REGRESSION
    assert second is None  # deduped on the persisted marker
    alerts = await _payoff_alerts(db_session, project.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_payoff_transition_proven_with_volume_fires_info(db_session, mocker):
    """PROVEN with warm-start volume at/above the floor fires an INFO alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=10.0,
            cost_baseline=10.0,  # no cost regression
            warm_pool=POOL_PAYOFF_PROVEN_MIN_WARM_STARTS,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired == POOL_PAYOFF_PROVEN
    alerts = await _payoff_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO
    assert "PROVEN" in alerts[0].title


@pytest.mark.asyncio
async def test_payoff_transition_proven_below_volume_floor_no_alert(
    db_session, mocker
):
    """PROVEN but below the warm-start volume floor does NOT alert (too thin)."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=10.0,
            cost_baseline=10.0,
            warm_pool=POOL_PAYOFF_PROVEN_MIN_WARM_STARTS - 1,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired is None
    assert await _payoff_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_payoff_transition_no_lift_no_alert(db_session, mocker):
    """NO_LIFT (barely serving, cost in tolerance) is not alert-worthy."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=10.0,
            cost_baseline=10.0,
            warm_pool=1,
            pool_origin_share=0.02,
            hit_rate=0.01,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired is None
    assert await _payoff_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_payoff_transition_insufficient_data_no_alert(db_session, mocker):
    """INSUFFICIENT_DATA (too few pool dispatches to judge) is not alert-worthy."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=12.0,
            cost_baseline=10.0,
            warm_pool=8,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=5,  # below POOL_PAYOFF_MIN_POOL_DISPATCHES
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired is None
    assert await _payoff_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_payoff_transition_unscoped_project_no_alert(db_session, mocker):
    """A project NOT in the warm-pool scope is never evaluated/alerted."""
    import uuid as _uuid

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    # Scope the pool to some OTHER project so this one is out of scope.
    _runtime_config["warm_pool_project_allowlist"] = [str(_uuid.uuid4())]
    project = make_project(
        design_doc=_payoff_design_doc(
            incl_current=12.0,
            cost_baseline=10.0,
            warm_pool=8,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)
    assert fired is None
    assert await _payoff_alerts(db_session, project.id) == []


# ---------------------------------------------------------------------------
# Pre-enable counterfactual warm-pool hit-rate estimate
# ---------------------------------------------------------------------------

from app.efficiency_snapshot import (  # noqa: E402
    _estimate_pool_hit_rate,
    estimate_pool_hit_rate,
)


def _disp(label, second):
    """A (account_label, dispatched_at) tuple at ``second`` seconds past an epoch."""
    base = datetime(2026, 6, 1, 0, 0, 0)
    return (label, base + timedelta(seconds=second))


def test_estimate_pool_hit_rate_empty_returns_none():
    out = _estimate_pool_hit_rate(
        [], target_size=3, replenish_seconds=120, idle_ttl_seconds=1800
    )
    assert out["total_dispatches"] == 0
    assert out["estimated_hit_rate"] is None
    assert out["by_account"] == {}


def test_estimate_pool_hit_rate_target_zero_all_cold():
    # Zero capacity -> every dispatch is a miss -> hit rate 0.0 (not None: we
    # have dispatches, just no warm capacity).
    disp = [_disp("acct-a", s) for s in (0, 1000, 2000)]
    out = _estimate_pool_hit_rate(
        disp, target_size=0, replenish_seconds=120, idle_ttl_seconds=1800
    )
    assert out["total_dispatches"] == 3
    assert out["estimated_warm_hits"] == 0
    assert out["estimated_hit_rate"] == 0.0


def test_estimate_pool_hit_rate_large_target_serves_all_when_spread():
    # One member per simultaneous arrival -> all warm.
    disp = [_disp("acct-a", s) for s in (0, 1, 2, 3)]
    out = _estimate_pool_hit_rate(
        disp, target_size=10, replenish_seconds=120, idle_ttl_seconds=1800
    )
    assert out["estimated_hit_rate"] == 1.0


def test_estimate_pool_hit_rate_cross_account_is_miss_without_retarget():
    # Pool target 1, single demand-active account 'a' gets the slot; a dispatch
    # on a different account 'b' has no compatible idle member -> miss.
    disp = [_disp("acct-a", 0), _disp("acct-a", 1000), _disp("acct-b", 2000)]
    out = _estimate_pool_hit_rate(
        disp, target_size=1, replenish_seconds=120, idle_ttl_seconds=1800
    )
    # 'a' is busiest (2 dispatches) so it owns the single slot; both its
    # well-spaced dispatches are warm, 'b' misses.
    assert out["by_account"]["acct-a"]["estimated_warm_hits"] == 2
    assert out["by_account"]["acct-b"]["estimated_warm_hits"] == 0
    assert out["estimated_warm_hits"] == 2


def test_estimate_pool_hit_rate_retarget_covers_other_account():
    # Same history, but with retarget the single shared member can serve EITHER
    # account, so the 'b' dispatch (well after 'a's are done) also lands warm.
    disp = [_disp("acct-a", 0), _disp("acct-a", 1000), _disp("acct-b", 2000)]
    pinned = _estimate_pool_hit_rate(
        disp, target_size=1, replenish_seconds=120, idle_ttl_seconds=1800
    )
    retarget = _estimate_pool_hit_rate(
        disp,
        target_size=1,
        replenish_seconds=120,
        idle_ttl_seconds=1800,
        account_retarget=True,
    )
    assert retarget["estimated_warm_hits"] >= pinned["estimated_warm_hits"]
    assert retarget["estimated_warm_hits"] == 3


def test_estimate_pool_hit_rate_bursty_below_steady():
    # Bursty: 3 arrivals within one replenish window exceed a single member.
    bursty = [_disp("acct-a", s) for s in (0, 1, 2)]
    # Steady: same count spread far apart (and within TTL) -> all warm.
    steady = [_disp("acct-a", s) for s in (0, 1000, 2000)]
    burst_out = _estimate_pool_hit_rate(
        bursty, target_size=1, replenish_seconds=500, idle_ttl_seconds=100000
    )
    steady_out = _estimate_pool_hit_rate(
        steady, target_size=1, replenish_seconds=500, idle_ttl_seconds=100000
    )
    assert burst_out["estimated_hit_rate"] < steady_out["estimated_hit_rate"]
    assert burst_out["estimated_warm_hits"] == 1  # only the first burst arrival
    assert steady_out["estimated_hit_rate"] == 1.0


def test_estimate_pool_hit_rate_increases_with_target_size():
    # Burst of 3 near-simultaneous arrivals; more slots -> more warm hits.
    disp = [_disp("acct-a", s) for s in (0, 1, 2)]
    small = _estimate_pool_hit_rate(
        disp, target_size=1, replenish_seconds=500, idle_ttl_seconds=1800
    )
    big = _estimate_pool_hit_rate(
        disp, target_size=3, replenish_seconds=500, idle_ttl_seconds=1800
    )
    assert big["estimated_hit_rate"] > small["estimated_hit_rate"]


def test_estimate_pool_hit_rate_increases_with_ttl():
    # Sparse, steady arrivals with gaps far larger than a short TTL: a short TTL
    # reaps the member between arrivals (cold re-provision gap), a long TTL keeps
    # it warm. Longer TTL -> higher hit rate.
    disp = [_disp("acct-a", s) for s in (0, 1000, 2000, 3000)]
    short_ttl = _estimate_pool_hit_rate(
        disp, target_size=1, replenish_seconds=500, idle_ttl_seconds=100
    )
    long_ttl = _estimate_pool_hit_rate(
        disp, target_size=1, replenish_seconds=500, idle_ttl_seconds=5000
    )
    assert long_ttl["estimated_hit_rate"] > short_ttl["estimated_hit_rate"]
    assert long_ttl["estimated_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_estimate_pool_hit_rate_wrapper_buckets_by_account(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Two dispatches on account a, one on account b.
    for secs, acct in ((300, "a"), (200, "a"), (100, "b")):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                vm_dispatched_at=now - timedelta(seconds=secs),
                context={"llm_account_id": acct},
            )
        )
    await db_session.commit()

    out = await estimate_pool_hit_rate(
        db_session, project.id, target_size=2, window_days=7
    )
    assert out["total_dispatches"] == 3
    assert out["target_size"] == 2
    assert set(out["by_account"].keys()) == {"a", "b"}
    assert "estimated_hit_rate" in out
    assert "account_retarget_scenario" in out
    assert "assumptions" in out


@pytest.mark.asyncio
async def test_pool_hit_rate_estimate_endpoint_returns_shape(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_dispatched_at=now - timedelta(minutes=5),
            context={"llm_account_id": "acct-a"},
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-hit-rate-estimate?target_size=2"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_size"] == 2
    assert data["total_dispatches"] == 1
    for key in (
        "estimated_hit_rate",
        "estimated_warm_hits",
        "by_account",
        "account_retarget_scenario",
        "assumptions",
        "replenish_seconds",
        "idle_ttl_seconds",
    ):
        assert key in data


@pytest.mark.asyncio
async def test_pool_hit_rate_estimate_endpoint_requires_membership(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # No membership.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-hit-rate-estimate?target_size=2"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pool_hit_rate_estimate_endpoint_validates_target_size(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    # target_size is required.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-hit-rate-estimate"
    )
    assert resp.status_code == 422
    # target_size above le=100 is rejected.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-hit-rate-estimate?target_size=101"
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# maybe_alert_unpriced_models_coverage — unpriced-models operator alert
# ---------------------------------------------------------------------------

from app.efficiency_snapshot import (  # noqa: E402
    LAST_ALERTED_UNPRICED_KEY,
    UNPRICED_ALERT_MIN_TASKS,
    maybe_alert_unpriced_models_coverage,
)


def _priced_task(project_id):
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 1000, "total_output_tokens": 500},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )


def _unpriced_task(project_id, model="fake-model-x"):
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 500, "total_output_tokens": 200},
        provider="fakeprovider",
        model=model,
    )


async def _unpriced_alerts(db_session, project_id):
    stmt = _sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("unpriced models"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_unpriced_alert_fires_when_coverage_degrades(db_session, mocker):
    """Crossing the unpriced-share threshold fires one WARNING naming the combos."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 2 priced + 4 unpriced -> unpriced share = 4/6 = 0.67, well over threshold.
    for _ in range(2):
        db_session.add(_priced_task(project.id))
    for _ in range(4):
        db_session.add(_unpriced_task(project.id))
    await db_session.commit()

    fired = await maybe_alert_unpriced_models_coverage(db_session, project.id)
    assert fired is True

    alerts = await _unpriced_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "fakeprovider:fake-model-x" in alerts[0].message
    assert "67%" in alerts[0].message
    bcast.assert_awaited()

    await db_session.refresh(project)
    assert project.design_doc[DESIGN_DOC_KEY][LAST_ALERTED_UNPRICED_KEY] == "degraded"


@pytest.mark.asyncio
async def test_unpriced_alert_no_fire_when_coverage_full(db_session, mocker):
    """Full pricing coverage (no unpriced tasks) never alerts."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(5):
        db_session.add(_priced_task(project.id))
    await db_session.commit()

    fired = await maybe_alert_unpriced_models_coverage(db_session, project.id)
    assert fired is None
    assert await _unpriced_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_unpriced_alert_dedups_on_stable_degraded_state(db_session, mocker):
    """A second tick at the same degraded state does not fire a second alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_priced_task(project.id))
    for _ in range(4):
        db_session.add(_unpriced_task(project.id))
    await db_session.commit()

    first = await maybe_alert_unpriced_models_coverage(db_session, project.id)
    second = await maybe_alert_unpriced_models_coverage(db_session, project.id)
    assert first is True
    assert second is None  # deduped on the persisted marker
    assert len(await _unpriced_alerts(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_unpriced_alert_ignores_tokenless_and_unattributed(db_session, mocker):
    """Token-less and unattributed (no provider/model) tasks never trip the alert.

    Many unpriced-LOOKING tasks here are excluded from the attributed
    denominator, so the unpriced share stays below threshold and nothing fires.
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One real unpriced task — below UNPRICED_ALERT_MIN_TASKS on its own.
    db_session.add(_unpriced_task(project.id))
    # Token-less tasks (no usage) — not priceable, must be ignored.
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                api_call_summary={"llm_call_count": 2},
                provider="fakeprovider",
                model="fake-model-x",
            )
        )
    # Token-bearing but unattributed (no provider/model) — must be ignored.
    for _ in range(5):
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                api_call_summary={
                    "total_input_tokens": 300,
                    "total_output_tokens": 100,
                },
            )
        )
    await db_session.commit()

    assert UNPRICED_ALERT_MIN_TASKS > 1  # guards the single-unpriced premise
    fired = await maybe_alert_unpriced_models_coverage(db_session, project.id)
    assert fired is None
    assert await _unpriced_alerts(db_session, project.id) == []


def _make_timeout_task(project_id, subtype: str):
    """A terminal FAILED task with persisted AGENT_TIMEOUT ground truth + started_at."""
    from app.failure_classification import AGENT_TIMEOUT

    now = _utcnow()
    return make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        started_at=now - timedelta(minutes=10),
        updated_at=now,
        context={"failure_category": AGENT_TIMEOUT, "timeout_subtype": subtype},
    )


def _make_format_compliance_task(
    project_id,
    *,
    errors: int,
    repair_attempts: int = 0,
    repair_successes: int = 0,
):
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        format_compliance_summary={
            "format_error_total": errors,
            "format_repair_attempts": repair_attempts,
            "format_repair_successes": repair_successes,
        },
    )


@pytest.mark.asyncio
async def test_agent_timeout_rate_persisted_and_drifted(db_session):
    """The AGENT_TIMEOUT-rate measure is folded into the snapshot and drifts vs baseline."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First window: 2 oom timeouts among 4 execution-reached tasks -> 0.5 overall.
    db_session.add(_make_timeout_task(project.id, "oom"))
    db_session.add(_make_timeout_task(project.id, "oom"))
    started = _make_efficient_task(project.id)
    started.started_at = _utcnow() - timedelta(minutes=10)
    db_session.add(started)
    started2 = _make_efficient_task(project.id)
    started2.started_at = _utcnow() - timedelta(minutes=10)
    db_session.add(started2)
    await db_session.commit()

    # First snapshot seeds the immutable baseline.
    snap = await write_snapshot(db_session, project.id)
    block = snap["agent_timeout_rate"]
    assert block["denominator"] == 4
    assert block["timeout_count"] == 2
    assert block["overall_rate"] == 0.5
    assert block["dominant_subtype"] == "oom"
    assert block["by_subtype"]["oom"]["rate"] == 0.5

    drift = await read_baseline_with_drift(db_session, project.id)
    atr = drift["agent_timeout_rate"]
    # Baseline == current on the seeding snapshot: zero drift.
    assert atr["overall"]["current"] == 0.5
    assert atr["overall"]["baseline"] == 0.5
    assert atr["overall"]["drift_pct"] == 0.0
    assert atr["dominant_subtype"] == "oom"
    assert atr["by_subtype"]["oom"]["current"] == 0.5
    assert atr["denominator_definition"]


@pytest.mark.asyncio
async def test_agent_timeout_rate_backfilled_into_preexisting_baseline(db_session):
    """A baseline that predates the measure gets agent_timeout_rate backfilled.

    agent_timeout_rate is a nested block, not a member of ALL_AXES, so the
    ALL_AXES-only re-seed loop never reaches it. Without an explicit backfill,
    any project whose efficiency_baseline was seeded before this measure -- the
    live, daily-snapshotted Blueprint project among them -- keeps a permanent
    baseline with no agent_timeout_rate, so read_baseline_with_drift yields
    baseline=None / drift_pct=None and the digest before/after sentence never
    appears. This asserts the next snapshot grafts the block into the immutable
    baseline.
    """
    # Pre-existing baseline lacks agent_timeout_rate entirely (predates measure).
    project = make_project(
        design_doc={
            "efficiency_baseline": {
                "baseline": {"captured_at": "2020-01-01T00:00:00+00:00"},
                "snapshots": [],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 1 oom timeout among 2 execution-reached tasks -> 0.5 overall.
    db_session.add(_make_timeout_task(project.id, "oom"))
    started = _make_efficient_task(project.id)
    started.started_at = _utcnow() - timedelta(minutes=10)
    db_session.add(started)
    await db_session.commit()

    # The else-branch (baseline already seeded) must still backfill the block.
    snap = await write_snapshot(db_session, project.id)
    assert snap["agent_timeout_rate"]["overall_rate"] == 0.5

    # write_snapshot mutates the project under project_lock but leaves the commit
    # to its caller (the snapshot service). Commit here so the subsequent refresh
    # reloads the persisted baseline rather than discarding the pending write.
    await db_session.commit()
    await db_session.refresh(project)
    baseline = project.design_doc["efficiency_baseline"]["baseline"]
    assert baseline.get("agent_timeout_rate") is not None
    assert baseline["agent_timeout_rate"]["overall_rate"] == 0.5

    drift = await read_baseline_with_drift(db_session, project.id)
    atr = drift["agent_timeout_rate"]
    assert atr["overall"]["baseline"] == 0.5
    assert atr["overall"]["drift_pct"] == 0.0


@pytest.mark.asyncio
async def test_snapshot_persists_format_compliance_block(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_format_compliance_task(project.id, errors=0))
    db_session.add(
        _make_format_compliance_task(
            project.id,
            errors=2,
            repair_attempts=2,
            repair_successes=1,
        )
    )
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    block = snap[FORMAT_COMPLIANCE_KEY]
    assert block["tasks_measured"] == 2
    assert block[FORMAT_COMPLIANCE_RATE_KEY] == 0.5
    assert block["format_error_total"] == 2
    assert block["repair_attempts"] == 2
    assert block["repair_successes"] == 1
    assert block[SELF_REPAIR_SUCCESS_RATE_KEY] == 0.5

    drift = await read_baseline_with_drift(db_session, project.id)
    fc = drift[FORMAT_COMPLIANCE_KEY]
    assert fc["tasks_measured"] == 2
    assert fc[FORMAT_COMPLIANCE_RATE_KEY]["current"] == 0.5
    assert fc[FORMAT_COMPLIANCE_RATE_KEY]["baseline"] == 0.5
    assert fc[FORMAT_COMPLIANCE_RATE_KEY]["drift_pct"] == 0.0
    assert fc[FORMAT_COMPLIANCE_RATE_KEY]["history"] == [
        {"captured_at": snap["captured_at"], "value": 0.5}
    ]


@pytest.mark.asyncio
async def test_format_compliance_backfilled_into_preexisting_baseline(db_session):
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {"captured_at": "2020-01-01T00:00:00+00:00"},
                "snapshots": [],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_format_compliance_task(project.id, errors=0))
    db_session.add(_make_format_compliance_task(project.id, errors=1))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    assert snap[FORMAT_COMPLIANCE_KEY][FORMAT_COMPLIANCE_RATE_KEY] == 0.5

    await db_session.commit()
    await db_session.refresh(project)
    baseline = project.design_doc[DESIGN_DOC_KEY]["baseline"]
    assert baseline[FORMAT_COMPLIANCE_KEY][FORMAT_COMPLIANCE_RATE_KEY] == 0.5

    drift = await read_baseline_with_drift(db_session, project.id)
    assert drift[FORMAT_COMPLIANCE_KEY][FORMAT_COMPLIANCE_RATE_KEY]["baseline"] == 0.5
    assert drift[FORMAT_COMPLIANCE_KEY][FORMAT_COMPLIANCE_RATE_KEY]["drift_pct"] == 0.0


@pytest.mark.asyncio
async def test_efficiency_axes_endpoint_exposes_format_compliance(
    client: AsyncClient, db_session
):
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    db_session.add(_make_format_compliance_task(project.id, errors=0))
    db_session.add(
        _make_format_compliance_task(
            project.id,
            errors=1,
            repair_attempts=1,
            repair_successes=1,
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/efficiency-axes")
    assert resp.status_code == 200
    body = resp.json()
    fc = body[FORMAT_COMPLIANCE_KEY]
    assert fc["tasks_measured"] == 2
    assert fc["format_error_total"] == 1
    assert fc[FORMAT_COMPLIANCE_RATE_KEY]["current"] == 0.5
    assert fc[SELF_REPAIR_SUCCESS_RATE_KEY]["current"] == 1.0


@pytest.mark.asyncio
async def test_efficiency_axes_endpoint_exposes_timeout_rate(
    client: AsyncClient, db_session
):
    """The efficiency-axes endpoint surfaces the agent_timeout_rate block."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    db_session.add(_make_timeout_task(project.id, "execution_timeout"))
    started = _make_efficient_task(project.id)
    started.started_at = _utcnow() - timedelta(minutes=10)
    db_session.add(started)
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/efficiency-axes")
    assert resp.status_code == 200
    body = resp.json()
    assert "agent_timeout_rate" in body
    atr = body["agent_timeout_rate"]
    assert atr["timeout_count"] == 1
    assert atr["denominator"] == 2
    assert atr["overall"]["current"] == 0.5


# ---------------------------------------------------------------------------
# Standalone-smoke-compatible invariant tests (no DB session required)
# ---------------------------------------------------------------------------


def test_all_axes_have_named_consumer_or_retired_record():
    """Every ALL_AXES member has an entry in AXIS_CONSUMERS with a non-empty
    consumer, pending, or retired disposition, and every entry claiming an
    active consumer is still a member of ALL_AXES (no stale records).

    This test runs under --standalone --no-postgres (no DB session needed).
    It is the distributed invariant guard: adding a new drift-alertable axis
    without a named consumer or retired-with-record annotation MUST fail CI.
    """
    all_axes_set = set(ALL_AXES)
    manifest_keys = set(AXIS_CONSUMERS)

    # (a) Every ALL_AXES member must appear in AXIS_CONSUMERS.
    missing = all_axes_set - manifest_keys
    assert not missing, (
        f"ALL_AXES members missing from AXIS_CONSUMERS: {sorted(missing)}. "
        "Add an entry with a 'consumer', 'pending', or 'retired'+'date' disposition."
    )

    # (b) Every manifest entry must have at least one valid disposition key.
    for axis_key, entry in AXIS_CONSUMERS.items():
        has_consumer = bool(entry.get("consumer"))
        has_pending = "pending" in entry
        has_retired = "retired" in entry and "date" in entry
        assert has_consumer or has_pending or has_retired, (
            f"AXIS_CONSUMERS['{axis_key}'] has no valid disposition. "
            "Provide 'consumer', 'pending', or both 'retired' and 'date'."
        )

    # (c) Every manifest key claiming an active consumer must still be in ALL_AXES
    # (prevents stale consumer records from outliving their axis).
    stale = {
        k for k, v in AXIS_CONSUMERS.items() if v.get("consumer") and k not in all_axes_set
    }
    assert not stale, (
        f"AXIS_CONSUMERS entries claiming active consumers for axes no longer in ALL_AXES: "
        f"{sorted(stale)}. Remove or update the manifest entry."
    )


def test_nested_snapshot_metrics_have_ground_truth_and_named_consumer():
    required = {"agent_timeout_rate", FORMAT_COMPLIANCE_KEY}
    missing = required - set(NESTED_METRIC_CONSUMERS)
    assert not missing, (
        f"Nested snapshot metrics missing governance entries: {sorted(missing)}"
    )
    for metric_key in required:
        entry = NESTED_METRIC_CONSUMERS[metric_key]
        assert entry.get("ground_truth"), (
            f"NESTED_METRIC_CONSUMERS['{metric_key}'] must name ground_truth"
        )
        assert entry.get("consumer"), (
            f"NESTED_METRIC_CONSUMERS['{metric_key}'] must name consumer"
        )


def test_axis_llm_cost_constant_value():
    """AXIS_LLM_COST_PER_MERGED_PR resolves to the expected string key."""
    assert AXIS_LLM_COST_PER_MERGED_PR == "llm_cost_per_merged_pr"


# ---------------------------------------------------------------------------
# compute_llm_cost_by_task_type tests
# ---------------------------------------------------------------------------


def test_llm_cost_breakdown_by_task_type_basic():
    """Tasks with distinct payload types bucket into separate rows; totals
    reconcile with compute_llm_cost_breakdown's overall total_cost_usd."""
    import uuid

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # 'create' task: 2000 in, 1000 out — cost = 2000*3e-6 + 1000*15e-6 = 0.021
    create_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # 'review' task: 1000 in, 500 out — cost = 1000*3e-6 + 500*15e-6 = 0.0105
    review_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([create_task, review_task])
    by_type = {r["task_type"]: r for r in rows}

    assert set(by_type) == {"create", "review"}

    create_row = by_type["create"]
    expected_create_cost = round(2000 * input_rate + 1000 * output_rate, 6)
    assert create_row["total_cost_usd"] == expected_create_cost
    assert create_row["total_input_tokens"] == 2000
    assert create_row["total_output_tokens"] == 1000
    assert create_row["llm_call_count"] == 8
    assert create_row["task_count"] == 1
    assert create_row["merged_count"] == 1
    assert create_row["merge_rate"] == 1.0
    assert create_row["verified_merged_count"] == 1
    assert create_row["verified_merge_rate"] == 1.0

    review_row = by_type["review"]
    expected_review_cost = round(1000 * input_rate + 500 * output_rate, 6)
    assert review_row["total_cost_usd"] == expected_review_cost
    assert review_row["merged_count"] == 0
    assert review_row["merge_rate"] == 0.0

    # share_of_total_cost must sum to 1.0
    share_sum = sum(r["share_of_total_cost"] or 0.0 for r in rows)
    assert abs(share_sum - 1.0) < 1e-6

    # Total cost must reconcile with compute_llm_cost_breakdown
    breakdown_total = compute_llm_cost_breakdown([create_task, review_task])["totals"]["total_cost_usd"]
    type_total = round(sum(r["total_cost_usd"] for r in rows), 6)
    assert type_total == breakdown_total

    # Sorted cost descending: create (0.021) before review (0.0105)
    assert rows[0]["task_type"] == "create"
    assert rows[1]["task_type"] == "review"


def test_llm_cost_breakdown_by_task_type_no_type_field():
    """Tasks with no 'type' key in first_payload fall into 'unspecified' bucket."""
    import uuid

    project_id = uuid.uuid4()

    # Payload has no 'type' key
    no_type = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"effort": "high"}],
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 200,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Empty payloads list
    empty_payload = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[],
        api_call_summary={
            "total_input_tokens": 300,
            "total_output_tokens": 100,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([no_type, empty_payload])
    assert len(rows) == 1
    assert rows[0]["task_type"] == "unspecified"
    assert rows[0]["task_count"] == 2
    assert rows[0]["total_input_tokens"] == 800
    assert rows[0]["total_output_tokens"] == 300


def test_llm_cost_breakdown_by_task_type_merge_credited_once():
    """A multi-attempt merged task credits merged_count once to its type bucket,
    not once per attempt-row (regression guard for the per-model merge-once pattern)."""
    import uuid

    project_id = uuid.uuid4()

    now = _utcnow()
    from app.models import bank_task_current_attempt

    task = make_task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": True},
        payloads=[{"type": "fix"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
            "llm_call_count": 3,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        vm_provisioned_at=now - timedelta(minutes=9),
        vm_spot=True,
    )
    bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))

    # Second attempt on the same model
    task.status = TaskStatus.COMPLETED
    task.vm_provisioned_at = now - timedelta(minutes=3)
    task.vm_stopped_at = now
    task.vm_spot = False
    task.api_call_summary = {
        "total_input_tokens": 250,
        "total_output_tokens": 150,
        "llm_call_count": 2,
    }

    rows = compute_llm_cost_by_task_type([task])
    assert len(rows) == 1
    fix_row = rows[0]
    assert fix_row["task_type"] == "fix"
    assert fix_row["task_count"] == 1
    # Merged once, not once per attempt
    assert fix_row["merged_count"] == 1
    assert fix_row["merge_rate"] == 1.0
    assert fix_row["verified_merged_count"] == 1
    # Cost includes both attempts
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_cost = round((1000 + 250) * input_rate + (400 + 150) * output_rate, 6)
    assert fix_row["total_cost_usd"] == expected_cost


@pytest.mark.asyncio
async def test_llm_cost_breakdown_endpoint_includes_by_task_type(
    client: AsyncClient, db_session
):
    """The /llm-cost-breakdown endpoint must include a 'by_task_type' list."""
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
            context={"pr_merged": True},
            payloads=[{"type": "create"}],
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 5,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/llm-cost-breakdown")
    assert resp.status_code == 200
    data = resp.json()

    assert "by_task_type" in data
    by_type = {r["task_type"]: r for r in data["by_task_type"]}
    assert "create" in by_type
    row = by_type["create"]
    assert row["task_count"] == 1
    assert row["merged_count"] == 1
    assert row["total_input_tokens"] == 1000
    assert row["total_output_tokens"] == 500
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_cost = round(1000 * input_rate + 500 * output_rate, 6)
    assert row["total_cost_usd"] == expected_cost
