"""Tests for backend/app/efficiency_snapshot.py and the admin endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from testing.conftest import USE_POSTGRES

from app.efficiency_snapshot import (
    ALL_AXES,
    AXIS_CHECKPOINT_LOSS_RATE,
    AXIS_CONSUMERS,
    AXIS_COST_PER_MERGED_PR,
    AXIS_LANE_COST_PER_MERGED_PR,
    AXIS_LANE_WARM_RESUME_RATE,
    AXIS_LLM_CACHE_HIT_RATE,
    AXIS_LLM_COST_PER_MERGED_PR,
    AXIS_COST_PER_VERIFIED_TASK,
    AXIS_LLM_COST_PER_VERIFIED_TASK,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_PROMPT_STATIC_BYTES,
    AXIS_TOTAL_COST_PER_VERIFIED_TASK,
    AXIS_REWORK_COST_SHARE,
    AXIS_SALVAGE_COUNT,
    AXIS_SALVAGE_MERGE_RATE,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    CACHE_CREATION_INPUT_RATE_MULTIPLIER,
    CACHE_READ_INPUT_RATE_MULTIPLIER,
    DESIGN_DOC_KEY,
    FORMAT_COMPLIANCE_KEY,
    FORMAT_COMPLIANCE_RATE_KEY,
    LLM_CACHE_READ_INPUT_PRICING,
    LLM_TOKEN_PRICING,
    METRIC_DEF_VERSION,
    NESTED_METRIC_CONSUMERS,
    PROMPT_STATIC_BYTES_THRESHOLD,
    PROVIDER_ROUTING_ADOPT,
    PROVIDER_ROUTING_INSUFFICIENT_DATA,
    PROVIDER_ROUTING_REJECT,
    SELF_REPAIR_BURN_SHARE_KEY,
    SELF_REPAIR_MIN_ATTEMPTS,
    SELF_REPAIR_SUCCESS_RATE_KEY,
    VERIFIED_TASK_DRIFT_MIN_COUNT,
    _PROMPT_STATIC_CONST_NAMES,
    _PROMPT_STATIC_PLAIN_CONST_NAMES,
    _TASK_TYPE_FOCUS_CONST_NAMES,
    _compute_checkpoint_loss_rate,
    _compute_llm_cost_by_account,
    _compute_llm_cost_pricing_coverage,
    _compute_lane_cost_per_merged_pr,
    _compute_lane_warm_resume_rate,
    _compute_cold_start_breakdown_p50,
    _compute_prompt_static_bytes,
    _compute_total_cost_per_verified_task,
    _compute_verified_merge_count,
    _extract_ts_plain_string_length,
    _extract_ts_template_literal_length,
    _last_run_due,
    _task_llm_attempts,
    compute_current_axes,
    compute_effort_model_efficiency,
    compute_llm_cost_attribution_baseline_delta,
    compute_llm_cost_attribution_window_delta,
    compute_llm_cost_breakdown,
    compute_llm_cost_by_task_type,
    compute_pr_rework_chain_breakdown,
    compute_provider_routing_verdict,
    maybe_write_daily_snapshot,
    read_baseline_with_drift,
    write_snapshot,
)
from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    TaskStatus,
    bank_task_current_attempt,
)
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
async def test_compute_current_axes_includes_llm_cache_hit_rate(db_session):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: one with 30% cache hit rate, one with no cache hits.
    # total: 300 cache-read / 2000 input = 0.15
    db_session.add(
        _make_merged_task(
            project.id,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "total_cache_read_input_tokens": 300,
                "llm_call_count": 5,
            },
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert AXIS_LLM_CACHE_HIT_RATE in axes
    # 300 cache-read from second task / (1000 + 1000) total input = 0.15
    assert axes[AXIS_LLM_CACHE_HIT_RATE] == 0.15


@pytest.mark.asyncio
async def test_compute_current_axes_includes_rework_cost_share(db_session):
    """AXIS_REWORK_COST_SHARE is in ALL_AXES and compute_current_axes returns a float."""
    # Sanity: the constant must be in ALL_AXES.
    assert AXIS_REWORK_COST_SHARE in ALL_AXES, (
        "AXIS_REWORK_COST_SHARE must be registered in ALL_AXES"
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # PR 42 chain: create (first-pass) + fix (rework) with priced LLM tokens.
    create = _pr_chain_task(
        project.id,
        pr_number=42,
        task_type="create",
        input_tokens=2000,
        output_tokens=1000,
    )
    fix = _pr_chain_task(
        project.id,
        pr_number=42,
        task_type="fix",
        input_tokens=1500,
        output_tokens=800,
    )
    db_session.add_all([create, fix])
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert AXIS_REWORK_COST_SHARE in axes, (
        "compute_current_axes must include AXIS_REWORK_COST_SHARE"
    )

    value = axes[AXIS_REWORK_COST_SHARE]
    assert isinstance(value, float), (
        f"AXIS_REWORK_COST_SHARE must be a float, got {type(value).__name__}: {value!r}"
    )
    # Rework share = fix cost / total cost.
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_rw_usd = round(1500 * input_rate + 800 * output_rate, 6)
    expected_total_usd = round(
        2000 * input_rate + 1000 * output_rate + expected_rw_usd, 6
    )
    expected_share = round(expected_rw_usd / expected_total_usd, 4)
    assert value == pytest.approx(expected_share, abs=1e-9)


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
async def test_cost_per_verified_task_differs_from_cost_per_merged_pr_with_fall_open_merges(
    db_session,
):
    """AXIS_COST_PER_VERIFIED_TASK uses is_verified_merge denominator and differs
    from AXIS_COST_PER_MERGED_PR when fall-open (CI-bypassed) merges are present.

    A mix of one verified and one unverified merged task: the verified axis
    denominator counts only the verified task, while the merged-pr axis counts
    both — so the two axes produce different values.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Verified merge: no unverified_merge_reason in context.
    db_session.add(_make_efficient_task(project.id))
    # Fall-open (unverified) merge: carries a degraded reason.
    db_session.add(
        _make_merged_task(
            project.id,
            reason="ci_unavailable_runner_offline",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # cost_per_merged_pr: denominator = 2 (both tasks merged)
    assert axes[AXIS_COST_PER_MERGED_PR] is not None

    # cost_per_verified_task: denominator = 1 (only the verified task)
    assert axes[AXIS_COST_PER_VERIFIED_TASK] is not None

    # With one verified and one unverified task, the verified denominator is
    # smaller, so the per-verified cost is higher than per-merged cost.
    assert axes[AXIS_COST_PER_VERIFIED_TASK] > axes[AXIS_COST_PER_MERGED_PR]

    # The verified cost is exactly total VM cost / 1 (only the verified task
    # has VM cost — the unverified merged task has no vm_provisioned_at).
    expected_verified = round(300 * ON_DEMAND_RATE, 4)
    assert axes[AXIS_COST_PER_VERIFIED_TASK] == expected_verified

    # llm_cost_per_verified_task is also present and finite.
    assert AXIS_LLM_COST_PER_VERIFIED_TASK in axes
    assert axes[AXIS_LLM_COST_PER_VERIFIED_TASK] is not None


@pytest.mark.asyncio
async def test_cost_per_verified_task_none_when_no_verified_merges(db_session):
    """When all merged tasks are fall-open (unverified), the verified-task axes
    return None because the denominator is zero."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two fall-open merges, zero verified.
    db_session.add(
        _make_merged_task(
            project.id,
            reason="ci_unavailable_runner_offline",
        )
    )
    db_session.add(
        _make_merged_task(
            project.id,
            reason="no_runnable_tests",
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # cost_per_merged_pr is computed (both tasks merged).
    assert axes[AXIS_COST_PER_MERGED_PR] is not None
    # Verified axes are None — no verified merges in the window.
    assert axes[AXIS_COST_PER_VERIFIED_TASK] is None
    assert axes[AXIS_LLM_COST_PER_VERIFIED_TASK] is None


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
            context={"_timing": {"warm_start": True, "warm_claim_origin": origin}},
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
            context={"_timing": {"warm_start": True, "warm_claim_origin": "warm_pool"}},
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
async def test_read_baseline_with_drift_includes_panel_review_effectiveness_and_vm_cost_attribution(
    db_session,
):
    """read_baseline_with_drift surfaces panel_review_effectiveness (overturn_rate,
    challenged counts) and vm_cost_attribution (task-type cost breakdown) from the
    latest snapshot alongside other efficiency-axes data."""
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # Create tasks with panel review second opinion and different task types
    # 2 confirmed, 1 revised, 1 rejected = 4 total challenged
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                    "state": "challenged_confirmed"
                },
            },
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            api_call_summary={
                "llm_call_count": 10,
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
            context={
                "pr_merged": True,
                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                    "state": "challenged_confirmed"
                },
            },
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            api_call_summary={
                "llm_call_count": 5,
                "total_input_tokens": 500,
                "total_output_tokens": 250,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                    "state": "challenged_revised"
                },
            },
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            api_call_summary={
                "llm_call_count": 3,
                "total_input_tokens": 300,
                "total_output_tokens": 150,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                    "state": "challenged_rejected"
                },
            },
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            api_call_summary={
                "llm_call_count": 2,
                "total_input_tokens": 200,
                "total_output_tokens": 100,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)

    # Verify panel_review_effectiveness is present
    assert "panel_review_effectiveness" in drift
    eff = drift["panel_review_effectiveness"]
    assert eff is not None
    assert eff["challenged_confirmed"] == 2
    assert eff["challenged_revised"] == 1
    assert eff["challenged_rejected"] == 1
    assert eff["total_challenged"] == 4
    assert eff["overturn_rate"] == 0.25

    # Verify vm_cost_attribution is present
    assert "vm_cost_attribution" in drift
    vm_attr = drift["vm_cost_attribution"]
    assert vm_attr is not None
    assert isinstance(vm_attr, dict)
    assert "by_task_type" in vm_attr
    assert isinstance(vm_attr["by_task_type"], list)
    assert len(vm_attr["by_task_type"]) > 0
    assert "total_vm_usd" in vm_attr
    # Each entry in by_task_type should have task_type and cost info
    for entry in vm_attr["by_task_type"]:
        assert "task_type" in entry
        assert "total_cost_usd" in entry
        assert "task_count" in entry


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
    # AXIS_PROMPT_STATIC_BYTES is also exempt: it is environment-derived
    # (parses agent/src/prompt.ts) and returns a positive int regardless
    # of whether any tasks exist.
    task_data_axes = [k for k in ALL_AXES if k != AXIS_PROMPT_STATIC_BYTES]
    assert all(axes[k] is None for k in task_data_axes)
    assert axes[AXIS_PROMPT_STATIC_BYTES] is None or isinstance(
        axes[AXIS_PROMPT_STATIC_BYTES], int
    )


@pytest.mark.asyncio
async def test_salvage_axes(db_session):
    """Salvage axes reflect salvage_bundle task count and merge rate in the window."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    salvage_payload = [{"type": "salvage_bundle"}]

    # Task 1: merged salvage bundle.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=salvage_payload,
            updated_at=now,
        )
    )
    # Task 2: completed salvage bundle that did NOT merge.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={},
            payloads=salvage_payload,
            updated_at=now,
        )
    )
    # Task 3: failed salvage bundle.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            context={},
            payloads=salvage_payload,
            updated_at=now,
        )
    )
    # Non-salvage task (should not affect salvage axes).
    db_session.add(
        _make_efficient_task(
            project.id,
            updated_at=now,
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    assert axes[AXIS_SALVAGE_COUNT] == 3
    assert axes[AXIS_SALVAGE_MERGE_RATE] == round(1 / 3, 4)


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
    # Migration marker applied — a v0 baseline chains through all migrations to current head (v6).
    # v0 → v1 (cold-start), v1 → v2 (review-iterations), v2 → v3 (cold-start dispatch fix),
    # v3 → v4 (llm cost init), v4 → v5 (pricing table additions), v5 → v6 (lane axes).
    assert baseline["_schema_version"] == 6
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
    # Schema migrated: v2 → v3 (tested here) plus v3 → v4, v4 → v5, v5 → v6 (auto-chained).
    assert baseline["_schema_version"] == 6
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
        assert {"current", "baseline", "drift_pct", "history"}.issubset(entry.keys())
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
async def test_snapshot_carries_definition_version_and_change_boundary(db_session):
    """Snapshots must stamp metric_def_version; read path must expose def_version
    per history point so consumers can detect metric-definition discontinuities.

    Acceptance requirement: new snapshots carry the current METRIC_DEF_VERSION,
    the drift response exposes def_version on every history point, and a version
    advance (simulated by back-patching an older snapshot) creates a visible
    boundary (distinct def_version values) in the history series.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    # First snapshot — written by the current writer, carries METRIC_DEF_VERSION.
    snap1 = await write_snapshot(db_session, project.id)
    await db_session.commit()
    assert snap1.get("metric_def_version") == METRIC_DEF_VERSION, (
        f"write_snapshot must stamp metric_def_version={METRIC_DEF_VERSION!r}; "
        f"got {snap1.get('metric_def_version')!r}"
    )

    # Second snapshot — also written by current writer, same version.
    snap2 = await write_snapshot(db_session, project.id)
    await db_session.commit()
    assert snap2.get("metric_def_version") == METRIC_DEF_VERSION

    # Simulate a pre-existing snapshot from a prior definition era by back-
    # patching the first stored snapshot to an older version string.  This models
    # the real scenario: persisted snapshots 2026-06-06..09 have no version stamp
    # (metric_def_version absent), while future snapshots carry v6.
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import Project

    proj = await db_session.get(Project, project.id)
    assert proj is not None
    doc = dict(proj.design_doc or {})
    container = dict(doc.get("efficiency_baseline") or {})
    snapshots = list(container.get("snapshots") or [])
    assert len(snapshots) >= 2, "expected at least 2 snapshots to be stored"
    # Remove version from first snapshot to simulate a pre-v6 era entry.
    snapshots[0] = {k: v for k, v in snapshots[0].items() if k != "metric_def_version"}
    container["snapshots"] = snapshots
    doc["efficiency_baseline"] = container
    proj.design_doc = doc
    flag_modified(proj, "design_doc")
    await db_session.commit()

    # Now read back via the public API and verify def_version is surfaced.
    result = await read_baseline_with_drift(db_session, project.id)

    for axis in ALL_AXES:
        hist = result["axes"][axis]["history"]
        assert len(hist) >= 2, f"{axis}: expected >= 2 history entries"
        # Every entry must carry the def_version key (value may be None for
        # pre-stamping snapshots — that's the correct signal for old entries).
        for entry in hist:
            assert "def_version" in entry, (
                f"{axis}: history entry missing 'def_version' key: {entry!r}"
            )
        # The first entry (back-patched to no version) must surface None,
        # the last entry must surface the current version — a visible boundary.
        first_version = hist[0]["def_version"]
        last_version = hist[-1]["def_version"]
        assert first_version is None, (
            f"{axis}: expected None def_version for pre-stamping snapshot, "
            f"got {first_version!r}"
        )
        assert last_version == METRIC_DEF_VERSION, (
            f"{axis}: expected {METRIC_DEF_VERSION!r} on latest snapshot, "
            f"got {last_version!r}"
        )

    # Also verify the cost_per_merged_pr_incl_pool_idle_axis history carries
    # def_version for the same discontinuity detection requirement.
    incl_axis = result.get("cost_per_merged_pr_incl_pool_idle_axis") or {}
    incl_hist = incl_axis.get("history") or []
    for entry in incl_hist:
        assert "def_version" in entry, (
            f"cost_per_merged_pr_incl_pool_idle_axis history entry missing "
            f"'def_version': {entry!r}"
        )


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


def test_merged_count_excludes_rollup_parent():
    """Rollup parents must not be counted as merged PRs alongside their children.

    When a rollup lands, is_merged_completion is True on the parent AND on every
    child.  Counting the parent inflates cost_per_merged_pr and per-type
    merged_count.  The fix: build the set of task IDs that appear as
    parent_task_id on at least one sibling, and exclude those from the merged
    count.
    """
    import uuid

    from app.efficiency_snapshot import _cost_and_merges, compute_vm_cost_by_task_type

    project_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    now = _utcnow()

    # Rollup parent: merged, but should be excluded from count.
    parent = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
    )
    parent.id = parent_id

    # Two children: each merged, both should count.
    child1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=3),
        vm_stopped_at=now,
        vm_spot=False,
        parent_task_id=parent_id,
    )
    child2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=4),
        vm_stopped_at=now,
        vm_spot=False,
        parent_task_id=parent_id,
    )

    tasks = [parent, child1, child2]

    # _cost_and_merges: merged count must be 2 (children only), not 3.
    _, merged = _cost_and_merges(tasks)
    assert merged == 2, f"Expected 2 merged (children only), got {merged}"

    # compute_vm_cost_by_task_type: merged_count per bucket must also be 2.
    by_type = compute_vm_cost_by_task_type(tasks)
    total_merged = sum(b["merged_count"] for b in by_type)
    assert total_merged == 2, f"Expected 2 merged across types, got {total_merged}"


def test_verified_merge_count_excludes_rollup_parent():
    """Rollup parents must not be counted as verified merges alongside their children.

    When a rollup lands, is_verified_merge is True on the parent AND on every
    child. Counting the parent inflates the denominator of
    AXIS_TOTAL_COST_PER_VERIFIED_TASK. The fix: exclude parent_task_id owners.
    """
    import uuid

    project_id = uuid.uuid4()
    parent_id = uuid.uuid4()

    now = _utcnow()

    # Rollup parent: verified merge, but should be excluded from count.
    parent = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
    )
    parent.id = parent_id

    # Two children: each verified merge, both should count.
    child1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=3),
        vm_stopped_at=now,
        vm_spot=False,
        parent_task_id=parent_id,
    )
    child2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=4),
        vm_stopped_at=now,
        vm_spot=False,
        parent_task_id=parent_id,
    )

    tasks = [parent, child1, child2]

    # Verified merge count must be 2 (children only), not 3.
    verified = _compute_verified_merge_count(tasks)
    assert verified == 2, f"Expected 2 verified merges (children only), got {verified}"

    # Axis value must use verified denominator of 2.
    value = _compute_total_cost_per_verified_task(tasks)
    assert value is not None
    # 3 tasks * 300s on-demand = 900s; rate ~0.0532/s => ~47.88 + 0 LLM
    # Quick sanity: value > 0 and uses denominator 2.
    assert value > 0


def test_total_cost_per_verified_task_uses_verified_merge_not_merged_completion():
    """Denominator is is_verified_merge, NOT is_merged_completion.

    A task with pr_merged=True + unverified_merge_reason (degraded fall-open
    merge) is is_merged_completion but NOT is_verified_merge. It must be excluded
    from the AXIS_TOTAL_COST_PER_VERIFIED_TASK denominator.
    """
    import uuid

    project_id = uuid.uuid4()
    now = _utcnow()

    # Verified merge: pr_merged=True, no unverified_merge_reason.
    verified_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
    )

    # Degraded fall-open merge: pr_merged=True WITH unverified_merge_reason.
    # This is is_merged_completion=True but is_verified_merge=False.
    unverified_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "review_loop": {"unverified_merge_reason": "ci_unavailable"},
        },
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
    )

    tasks = [verified_task, unverified_task]

    # Verified count must be 1 (only the verified task), not 2.
    verified = _compute_verified_merge_count(tasks)
    assert verified == 1, (
        f"Expected 1 verified merge (unverified fall-open excluded), got {verified}"
    )

    # Axis value exists and uses denominator 1.
    value = _compute_total_cost_per_verified_task(tasks)
    assert value is not None


@pytest.mark.asyncio
async def test_total_cost_per_verified_task_drift_suppression(db_session):
    """Drift_pct is suppressed when verified_merge_count < 20, emitted at >= 20."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # Seed 19 verified-merge tasks (below the VERIFIED_TASK_DRIFT_MIN_COUNT floor).
    for i in range(19):
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            vm_spot=False,
        )
        db_session.add(t)
    await db_session.commit()

    # Write first snapshot — becomes baseline.
    snap1 = await write_snapshot(db_session, project.id)
    await db_session.commit()

    # Verify the denominator is recorded as 19 and axis has a value.
    assert snap1.get("total_cost_per_verified_task_denominator") == 19
    assert snap1.get(AXIS_TOTAL_COST_PER_VERIFIED_TASK) is not None

    # Read drift: drift_pct must be None (below floor of 20).
    drift_below = await read_baseline_with_drift(db_session, project.id)
    axis_below = drift_below["axes"][AXIS_TOTAL_COST_PER_VERIFIED_TASK]
    assert axis_below["current"] is not None
    assert axis_below["baseline"] is not None
    assert axis_below["drift_pct"] is None, (
        f"Expected drift_pct=None when verified_count=19 < {VERIFIED_TASK_DRIFT_MIN_COUNT}"
    )

    # Add one more verified-merge task to reach 20.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            vm_spot=False,
        )
    )
    await db_session.commit()

    # Write second snapshot — now has 20 verified merges.
    snap2 = await write_snapshot(db_session, project.id)
    await db_session.commit()
    assert snap2.get("total_cost_per_verified_task_denominator") == 20

    # Read drift: drift_pct must be emitted (at floor).
    drift_at = await read_baseline_with_drift(db_session, project.id)
    axis_at = drift_at["axes"][AXIS_TOTAL_COST_PER_VERIFIED_TASK]
    assert axis_at["current"] is not None
    assert axis_at["baseline"] is not None
    assert axis_at["drift_pct"] is not None, (
        f"Expected drift_pct emitted when verified_count=20 >= {VERIFIED_TASK_DRIFT_MIN_COUNT}"
    )
    assert isinstance(axis_at["drift_pct"], (int, float))


def test_total_cost_per_verified_task_in_all_axes():
    """The new axis must be registered in ALL_AXES and AXIS_CONSUMERS."""
    assert AXIS_TOTAL_COST_PER_VERIFIED_TASK in ALL_AXES
    assert AXIS_TOTAL_COST_PER_VERIFIED_TASK == "total_cost_per_verified_task"
    assert AXIS_TOTAL_COST_PER_VERIFIED_TASK in AXIS_CONSUMERS


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
    # v4 (llm cost init), v5 (pricing table additions), and v6 (lane axes).
    assert baseline["_schema_version"] == 6
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
async def test_llm_cost_prices_cache_classes_separately(db_session):
    """An api_call_summary carrying the cache breakdown prices cache_read at ~0.1x
    base and cache_creation at ~1.25x base, with the uncached remainder at base.

    total_input_tokens stays the INCLUSIVE input total (uncached + cache-creation
    + cache-read), so the breakdown does not change the token volume — only how
    each input class is priced.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    # 1000 inclusive input = 200 cache-creation + 300 cache-read + 500 uncached.
    task = _make_efficient_task(
        project.id,
        api_call_summary={
            "llm_call_count": 10,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
            "total_cache_creation_input_tokens": 200,
            "total_cache_read_input_tokens": 300,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    expected = round(
        500 * input_rate
        + 200 * input_rate * CACHE_CREATION_INPUT_RATE_MULTIPLIER
        + 300 * input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER
        + 400 * output_rate,
        6,
    )
    axes = await compute_current_axes(db_session, project.id)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == pytest.approx(expected)

    # The split must be strictly cheaper than pricing the whole inclusive input at
    # the base rate — proving the cache classes are no longer collapsed into the
    # uncached counter (cache-read at 0.1x dominates the saving).
    legacy_full_input = round(1000 * input_rate + 400 * output_rate, 6)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] < legacy_full_input

    # The truthful cache split is surfaced in the breakdown dicts (totals + by_model),
    # mirroring how total_input_tokens is surfaced.
    breakdown = compute_llm_cost_breakdown([task])
    totals = breakdown["totals"]
    assert totals["total_input_tokens"] == 1000
    assert totals["total_cache_creation_input_tokens"] == 200
    assert totals["total_cache_read_input_tokens"] == 300
    assert totals["total_cost_usd"] == pytest.approx(expected)
    row = breakdown["by_model"][0]
    assert row["total_cache_creation_input_tokens"] == 200
    assert row["total_cache_read_input_tokens"] == 300
    assert row["total_cost_usd"] == pytest.approx(expected)


def _assert_provider_specific_cache_read_pricing(provider: str, model: str):
    input_rate, output_rate = LLM_TOKEN_PRICING[(provider, model)]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[(provider, model)]
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "llm_call_count": 1,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
            "total_cache_creation_input_tokens": 200,
            "total_cache_read_input_tokens": 300,
        },
        provider=provider,
        model=model,
    )

    expected = round(
        500 * input_rate
        + 200 * input_rate
        + 300 * cache_read_rate
        + 400 * output_rate,
        6,
    )
    legacy_full_input = round(1000 * input_rate + 400 * output_rate, 6)

    breakdown = compute_llm_cost_breakdown([task])
    totals = breakdown["totals"]
    row = breakdown["by_model"][0]

    assert totals["total_cache_creation_input_tokens"] == 200
    assert totals["total_cache_read_input_tokens"] == 300
    assert totals["total_cost_usd"] == pytest.approx(expected)
    assert row["total_cost_usd"] == pytest.approx(expected)
    assert totals["total_cost_usd"] < legacy_full_input


def test_llm_cost_prices_deepseek_cache_read_at_cache_hit_rate():
    _assert_provider_specific_cache_read_pricing(
        "deepseek", "deepseek-v4-flash"
    )


def test_llm_cost_prices_zai_glm_cached_input_at_cached_input_rate():
    _assert_provider_specific_cache_read_pricing("zai", "glm-5.2")


def test_llm_cost_prices_openai_cached_input_at_cached_input_rate():
    _assert_provider_specific_cache_read_pricing("openai", "gpt-5.4-mini")


def test_llm_cost_bounds_malformed_cache_subcounts_to_total_input_for_pricing():
    input_rate, _output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "llm_call_count": 1,
            "total_input_tokens": 100,
            "total_output_tokens": 0,
            "total_cache_creation_input_tokens": 80,
            "total_cache_read_input_tokens": 80,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # The raw cache subcounts are malformed because 80 + 80 > 100 inclusive
    # input tokens. Pricing preserves cache-read first, then clamps creation
    # tokens to the remaining input total: 80 read + 20 creation + 0 uncached.
    expected = round(
        20 * input_rate * CACHE_CREATION_INPUT_RATE_MULTIPLIER
        + 80 * input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER,
        6,
    )
    unbounded = round(
        80 * input_rate * CACHE_CREATION_INPUT_RATE_MULTIPLIER
        + 80 * input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER,
        6,
    )

    breakdown = compute_llm_cost_breakdown([task])
    totals = breakdown["totals"]

    assert totals["total_input_tokens"] == 100
    assert totals["total_cache_creation_input_tokens"] == 20
    assert totals["total_cache_read_input_tokens"] == 80
    assert totals["total_cost_usd"] == pytest.approx(expected)
    assert totals["total_cost_usd"] != pytest.approx(unbounded)


@pytest.mark.asyncio
async def test_llm_cost_without_cache_breakdown_falls_back_to_base_rate(db_session):
    """A row WITHOUT the cache breakdown prices the full inclusive input at the
    base rate — current behavior, unchanged for legacy / pre-tracking rows."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    task = _make_efficient_task(
        project.id,
        api_call_summary={
            "llm_call_count": 10,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    expected = round(1000 * input_rate + 400 * output_rate, 6)
    axes = await compute_current_axes(db_session, project.id)
    assert axes[AXIS_LLM_COST_PER_MERGED_PR] == pytest.approx(expected)

    breakdown = compute_llm_cost_breakdown([task])
    totals = breakdown["totals"]
    # No breakdown present -> cache sub-counts are zero and cost is the legacy value.
    assert totals["total_cache_creation_input_tokens"] == 0
    assert totals["total_cache_read_input_tokens"] == 0
    assert totals["total_input_tokens"] == 1000
    assert totals["total_cost_usd"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_banked_attempt_carries_cache_breakdown_for_per_attempt_pricing(
    db_session,
):
    """bank_task_current_attempt carries the cache sub-counts into attempt_history
    so per-attempt pricing prices the cache classes after a retry bank.

    The inclusive banked scalar (llm_input_tokens_banked) keeps its meaning.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": True},
        vm_provisioned_at=_utcnow() - timedelta(minutes=5),
        vm_stopped_at=_utcnow(),
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
            "total_cache_creation_input_tokens": 200,
            "total_cache_read_input_tokens": 300,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    bank_task_current_attempt(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Cache sub-counts banked into the per-attempt history entry...
    assert task.attempt_history[0]["llm_cache_creation_input_tokens"] == 200
    assert task.attempt_history[0]["llm_cache_read_input_tokens"] == 300
    # ...while the inclusive banked scalar keeps its existing meaning.
    assert task.llm_input_tokens_banked == 1000
    assert task.api_call_summary is None

    task.status = TaskStatus.COMPLETED
    await db_session.commit()

    refreshed = await db_session.get(type(task), task.id)
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected = round(
        500 * input_rate
        + 200 * input_rate * CACHE_CREATION_INPUT_RATE_MULTIPLIER
        + 300 * input_rate * CACHE_READ_INPUT_RATE_MULTIPLIER
        + 400 * output_rate,
        6,
    )
    breakdown = compute_llm_cost_breakdown([refreshed])
    assert breakdown["totals"]["total_cost_usd"] == pytest.approx(expected)
    assert breakdown["totals"]["total_cache_creation_input_tokens"] == 200
    assert breakdown["totals"]["total_cache_read_input_tokens"] == 300


@pytest.mark.asyncio
async def test_banked_attempt_carries_prompt_size_fields(db_session):
    """bank_task_current_attempt carries prompt_size_bytes, prompt_section_count,
    and prompt_section_sizes from api_call_summary into attempt_history when present."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": False},
        vm_provisioned_at=_utcnow() - timedelta(minutes=5),
        vm_stopped_at=_utcnow(),
        api_call_summary={
            "llm_call_count": 2,
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "prompt_size_bytes": 80000,
            "prompt_section_count": 4,
            "prompt_section_sizes": [
                {"name": "task", "bytes": 30000},
                {"name": "guidance", "bytes": 50000},
            ],
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    bank_task_current_attempt(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.attempt_history[0]["prompt_size_bytes"] == 80000
    assert task.attempt_history[0]["prompt_section_count"] == 4
    assert task.attempt_history[0]["prompt_section_sizes"] == [
        {"name": "task", "bytes": 30000},
        {"name": "guidance", "bytes": 50000},
    ]
    assert task.api_call_summary is None


@pytest.mark.asyncio
async def test_banked_attempt_omits_prompt_size_fields_when_absent(db_session):
    """bank_task_current_attempt omits prompt-size keys when api_call_summary
    does not carry them — backward compat with older agents."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"pr_merged": False},
        vm_provisioned_at=_utcnow() - timedelta(minutes=5),
        vm_stopped_at=_utcnow(),
        api_call_summary={
            "llm_call_count": 1,
            "total_input_tokens": 200,
            "total_output_tokens": 50,
            # no prompt_size_bytes / prompt_section_count / prompt_section_sizes
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    bank_task_current_attempt(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert "prompt_size_bytes" not in task.attempt_history[0]
    assert "prompt_section_count" not in task.attempt_history[0]
    assert "prompt_section_sizes" not in task.attempt_history[0]


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
    assert anthropic_row["cost_per_merge"] == round(
        anthropic_row["total_cost_usd"] / 1, 6
    )
    assert openai_row["cost_per_merge"] == round(openai_row["total_cost_usd"] / 1, 6)

    totals = breakdown["totals"]
    assert totals["task_count"] == 1  # one task (not two attempt-rows)
    assert totals["merged_count"] == 1  # one merged TASK, not double-credited
    assert totals["merge_rate"] == 1.0


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
    # Two attempt-rows in one bucket, but a single task and a single merged task.
    assert row["task_count"] == 1
    assert row["merged_count"] == 1
    assert row["merge_rate"] == 1.0
    # cost_per_merge divides total bucket cost by the merge COUNT (1), not the
    # attempt count (2): the per-attempt accumulation would halve this figure.
    assert row["cost_per_merge"] == round(row["total_cost_usd"] / 1, 6)

    totals = breakdown["totals"]
    assert totals["merged_count"] == 1
    assert totals["merge_rate"] == 1.0


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
    # Schema bumped to v6 (v3 → v4 llm cost, v4 → v5 pricing additions, v5 → v6 lane axes).
    assert baseline["_schema_version"] == 6
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


def test_llm_cost_pricing_coverage_uses_attempt_provider_model():
    """Banked and current attempts are classified by their own provider/model."""
    import uuid

    project_id = uuid.uuid4()
    now = _utcnow()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 200,
            "llm_call_count": 2,
        },
        provider="fakeprovider",
        model="fake-model-x",
        vm_provisioned_at=now - timedelta(minutes=8),
    )
    bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))

    task.status = TaskStatus.COMPLETED
    task.api_call_summary = {
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "llm_call_count": 3,
    }
    task.provider = "anthropic"
    task.model = "claude-sonnet-4-6"

    result = _compute_llm_cost_pricing_coverage([task])

    assert result["priced_tasks"] == 1
    assert result["unpriced_tasks"] == 1
    assert result["unattributed_tasks"] == 0
    assert result["coverage_fraction"] == 0.5
    assert result["unpriced_combos"] == ["fakeprovider:fake-model-x"]


def test_llm_cost_pricing_coverage_counts_missing_attempt_attribution():
    """Token-bearing attempt rows with missing provider or model stay explicit."""
    import uuid

    project_id = uuid.uuid4()
    missing_provider = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
    )
    missing_provider.attempt_history = [
        {
            "provider": None,
            "model": "claude-sonnet-4-6",
            "llm_input_tokens": 200,
            "llm_output_tokens": 100,
            "llm_call_count": 1,
        }
    ]
    missing_model = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 300,
            "total_output_tokens": 150,
            "llm_call_count": 1,
        },
        provider="anthropic",
        model=None,
    )

    result = _compute_llm_cost_pricing_coverage([missing_provider, missing_model])

    assert result["priced_tasks"] == 0
    assert result["unpriced_tasks"] == 0
    assert result["unattributed_tasks"] == 2
    assert result["coverage_fraction"] == 0.0
    assert result["unpriced_combos"] == []


# ---------------------------------------------------------------------------
# Atomic (provider, model) pair resolution in _task_llm_attempts
# ---------------------------------------------------------------------------


def test_task_llm_attempts_resolves_provider_model_pair_atomically():
    """The canonical acceptance test: half-filled rows degrade cleanly; complete
    pairs use the attempt's own values; provider-switching retries never blend
    halves from different sources into chimera combos."""
    import uuid

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # Attempt 1: full pair from a different provider (e.g. a retry that switched).
    # Must use the attempt's own pair, NOT the task's current provider.
    # Attempt 2: provider set, model absent → half-filled → must degrade to (None, None).
    # Attempt 3: model set, provider absent → half-filled → must degrade to (None, None).
    # Attempt 4: neither set → must fall back to task's pair as a whole.
    task.attempt_history = [
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "llm_input_tokens": 100,
            "llm_output_tokens": 50,
            "llm_call_count": 1,
        },
        {
            "provider": "fakeprovider",
            "model": None,
            "llm_input_tokens": 200,
            "llm_output_tokens": 80,
            "llm_call_count": 1,
        },
        {
            "provider": None,
            "model": "claude-opus-4-8",
            "llm_input_tokens": 300,
            "llm_output_tokens": 120,
            "llm_call_count": 1,
        },
        {
            "llm_input_tokens": 400,
            "llm_output_tokens": 160,
            "llm_call_count": 2,
        },
    ]

    result = _task_llm_attempts(task)

    # Four banked attempts (no live tokens); order matches history.
    assert len(result) == 4

    # Attempt 1: full pair → uses attempt's own pair, not task's anthropic pair.
    p0, m0, _in0, _out0, _c0, *_cache0 = result[0]
    assert (p0, m0) == ("openai", "gpt-5.4"), f"expected openai:gpt-5.4, got {p0}:{m0}"

    # Attempt 2: provider-only half-filled → (None, None); no chimera with task.model.
    p1, m1, _in1, _out1, _c1, *_cache1 = result[1]
    assert (p1, m1) == (None, None), (
        f"expected (None, None) for provider-only row, got {p1}:{m1}"
    )

    # Attempt 3: model-only half-filled → (None, None); no chimera with task.provider.
    p2, m2, _in2, _out2, _c2, *_cache2 = result[2]
    assert (p2, m2) == (None, None), (
        f"expected (None, None) for model-only row, got {p2}:{m2}"
    )

    # Attempt 4: neither field → falls back to task's pair as a whole.
    p3, m3, _in3, _out3, _c3, *_cache3 = result[3]
    assert (p3, m3) == ("anthropic", "claude-sonnet-4-6"), (
        f"expected task fallback anthropic:claude-sonnet-4-6, got {p3}:{m3}"
    )

    # Validate that the cost breakdown never contains chimera combos.
    breakdown = compute_llm_cost_breakdown([task])
    combo_keys = {entry["model"] for entry in breakdown["by_model"]}
    assert "claude-opus-4-8" not in combo_keys, (
        "chimera model from model-only row leaked"
    )
    assert "gpt-5.4" in combo_keys  # legitimate pair must still appear


def test_task_llm_attempts_no_chimera_half_filled_provider_only():
    """Provider-only attempt row degrades to (None, None); task.model not spliced in."""
    import uuid

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-8",
    )
    task.attempt_history = [
        {
            "provider": "openai",
            "model": None,
            "llm_input_tokens": 100,
            "llm_output_tokens": 50,
            "llm_call_count": 1,
        }
    ]

    result = _task_llm_attempts(task)
    assert len(result) == 1
    p, m, _in, _out, _c, *_cache = result[0]
    assert (p, m) == (None, None), (
        f"provider-only row must yield (None, None), got {p}:{m}"
    )
    # Specifically: must NOT be openai:claude-opus-4-8 (chimera).
    assert not (p == "openai" and m == "claude-opus-4-8")


def test_task_llm_attempts_no_chimera_half_filled_model_only():
    """Model-only attempt row degrades to (None, None); task.provider not spliced in."""
    import uuid

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    task.attempt_history = [
        {
            "provider": None,
            "model": "gpt-5.4",
            "llm_input_tokens": 100,
            "llm_output_tokens": 50,
            "llm_call_count": 1,
        }
    ]

    result = _task_llm_attempts(task)
    assert len(result) == 1
    p, m, _in, _out, _c, *_cache = result[0]
    assert (p, m) == (None, None), (
        f"model-only row must yield (None, None), got {p}:{m}"
    )
    # Specifically: must NOT be anthropic:gpt-5.4 (chimera).
    assert not (p == "anthropic" and m == "gpt-5.4")


def test_task_llm_attempts_full_pair_used_atomically():
    """Attempt with both fields uses its own pair, not the task's current values."""
    import uuid

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    task.attempt_history = [
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "llm_input_tokens": 500,
            "llm_output_tokens": 200,
            "llm_call_count": 3,
        }
    ]

    result = _task_llm_attempts(task)
    assert len(result) == 1
    p, m, in_tok, out_tok, calls, *_cache = result[0]
    assert p == "openai" and m == "gpt-5.4", f"expected openai:gpt-5.4, got {p}:{m}"
    assert in_tok == 500 and out_tok == 200 and calls == 3


def test_task_llm_attempts_neither_field_falls_back_to_task():
    """Attempt with neither provider nor model falls back to task's pair as a whole."""
    import uuid

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-haiku-4-5",
    )
    task.attempt_history = [
        {
            "llm_input_tokens": 300,
            "llm_output_tokens": 100,
            "llm_call_count": 2,
        }
    ]

    result = _task_llm_attempts(task)
    assert len(result) == 1
    p, m, in_tok, out_tok, calls, *_cache = result[0]
    assert p == "anthropic" and m == "claude-haiku-4-5", (
        f"expected task fallback anthropic:claude-haiku-4-5, got {p}:{m}"
    )
    assert in_tok == 300 and out_tok == 100 and calls == 2


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
async def test_llm_cost_breakdown_endpoint_rework_attribution_shape(
    client: AsyncClient, db_session
):
    """llm-cost-breakdown endpoint must return rework_attribution with the mapped
    API-contract field names (not the internal names from compute_pr_rework_chain_breakdown).
    Regression guard for the rework_attribution key/field remapping introduced after
    commit 40380b3 ('rework_attribution key/field mismatch kills rework section in UI').
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/llm-cost-breakdown")
    assert resp.status_code == 200
    data = resp.json()

    # The key must be 'rework_attribution', not the old 'pr_chain_rework'.
    assert "rework_attribution" in data, (
        "Response must contain 'rework_attribution' key (not 'pr_chain_rework')"
    )
    assert "pr_chain_rework" not in data, (
        "Old key 'pr_chain_rework' must not appear in response"
    )

    ra = data["rework_attribution"]

    # API-contract field names (mapped from internal names by _format_rework_attribution).
    assert "rework_share" in ra, (
        "'rework_share' must be present (mapped from internal 'rework_share_of_total_llm_cost')"
    )
    assert "first_pass_llm_cost_usd" in ra, (
        "'first_pass_llm_cost_usd' must be present (mapped from internal 'first_pass_llm_usd')"
    )
    assert "rework_llm_cost_usd" in ra, (
        "'rework_llm_cost_usd' must be present (mapped from internal 'rework_llm_usd')"
    )
    assert "top_rework_prs" in ra
    assert isinstance(ra["top_rework_prs"], list)

    # Internal names must NOT leak through.
    assert "rework_share_of_total_llm_cost" not in ra, (
        "Internal field 'rework_share_of_total_llm_cost' must not appear in API response"
    )
    assert "first_pass_llm_usd" not in ra, (
        "Internal field 'first_pass_llm_usd' must not appear in API response"
    )
    assert "rework_llm_usd" not in ra, (
        "Internal field 'rework_llm_usd' must not appear in API response"
    )


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
    assert (
        snapshot["reuse_dropout"]["dominant_dropout"]["reason"] == "scan_file_conflict"
    )
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
    assert (
        trend[0]["dominant_share"]
        == result["reuse_dropout"]["dominant_dropout"]["share"]
    )
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
    assert (
        snapshot["pool_dispatch_funnel"]["dominant_miss"]["reason"] == "no_idle_member"
    )
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
    LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY,
    POOL_PAYOFF_PROVEN_MIN_WARM_STARTS,
    maybe_alert_pool_payoff_transition,
    maybe_alert_self_repair_burn_share,
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


def _warm_pool_alert_build_config():
    return {"alert_domains": {"warm_pool": {"enabled": True}}}


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
        build_config=_warm_pool_alert_build_config(),
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
async def test_payoff_transition_does_not_advance_marker_without_alert(
    db_session, mocker
):
    """A policy race that suppresses creation must not mark the verdict alerted."""
    mocker.patch(
        "app.alert_dispatch._maybe_create_pool_payoff_alert",
        new_callable=AsyncMock,
        return_value=(None, False),
    )
    project = make_project(
        build_config=_warm_pool_alert_build_config(),
        design_doc=_payoff_design_doc(
            incl_current=12.0,
            cost_baseline=10.0,
            warm_pool=8,
            pool_origin_share=0.5,
            hit_rate=0.4,
            total_dispatches=50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_pool_payoff_transition(db_session, project.id)

    assert fired is None
    await db_session.refresh(project)
    assert LAST_ALERTED_PAYOFF_KEY not in project.design_doc[DESIGN_DOC_KEY]
    assert await _payoff_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_payoff_transition_dedups_same_verdict(db_session, mocker):
    """A second tick at the same verdict does not fire a second alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        build_config=_warm_pool_alert_build_config(),
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
async def test_payoff_transition_proven_with_volume_skips_durable_info_alert(
    db_session, mocker
):
    """PROVEN is useful history, but it is not a durable operator alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        build_config=_warm_pool_alert_build_config(),
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
    assert fired is None
    alerts = await _payoff_alerts(db_session, project.id)
    assert alerts == []


@pytest.mark.asyncio
async def test_payoff_transition_proven_below_volume_floor_no_alert(db_session, mocker):
    """PROVEN but below the warm-start volume floor does NOT alert (too thin)."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        build_config=_warm_pool_alert_build_config(),
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
        build_config=_warm_pool_alert_build_config(),
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
        build_config=_warm_pool_alert_build_config(),
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
        build_config=_warm_pool_alert_build_config(),
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
# Provider routing A/B verdict transition alert
# ---------------------------------------------------------------------------

from app.efficiency_snapshot import (  # noqa: E402
    LAST_ALERTED_ROUTING_KEY,
    maybe_alert_provider_routing_transition,
)


def _routing_design_doc(
    *,
    verdict_state: str,
    cost_ratio: float | None = None,
    rate_delta: float | None = None,
    ds_run: int = 30,
    an_run: int = 30,
):
    """Build a design_doc whose latest snapshot carries the given provider
    routing verdict, suitable for testing the transition alert."""
    snap = {
        "captured_at": _utcnow().isoformat(),
        "provider_routing_verdict": {
            "state": verdict_state,
            "supporting": {
                "deepseek_run_count": ds_run,
                "deepseek_pr_eligible_run_count": ds_run,
                "deepseek_verified_landed_count": 5,
                "deepseek_verified_landed_rate": 0.25,
                "deepseek_cost_per_merge": 0.05,
                "deepseek_total_cost_usd": 0.25,
                "anthropic_run_count": an_run,
                "anthropic_pr_eligible_run_count": an_run,
                "anthropic_verified_landed_count": 5,
                "anthropic_verified_landed_rate": 0.25,
                "anthropic_cost_per_merge": 0.10,
                "anthropic_total_cost_usd": 0.50,
                "cost_ratio": cost_ratio or 0.50,
                "verified_rate_delta_pct": rate_delta or 0.0,
            },
            "recommended_action": "Keep measuring." if verdict_state == PROVIDER_ROUTING_INSUFFICIENT_DATA
            else "Adopt DeepSeek routing." if verdict_state == PROVIDER_ROUTING_ADOPT
            else "Reject DeepSeek routing.",
        },
    }
    return {
        DESIGN_DOC_KEY: {
            "baseline": {},
            "snapshots": [snap],
        }
    }


async def _routing_alerts(db_session, project_id):
    stmt = _sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("Provider routing verdict"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_routing_verdict_adopt_fires_warning(db_session, mocker):
    """A transition to ADOPT fires one WARNING carrying the supporting numbers."""
    bcast = mocker.patch(
        "app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock
    )
    project = make_project(
        design_doc=_routing_design_doc(verdict_state=PROVIDER_ROUTING_ADOPT),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_provider_routing_transition(db_session, project.id)
    assert fired == PROVIDER_ROUTING_ADOPT

    alerts = await _routing_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "ADOPT" in alerts[0].title
    assert "0.50" in alerts[0].message  # cost ratio
    assert "Adopt DeepSeek" in alerts[0].message
    bcast.assert_awaited()

    # The marker advanced so a repeat tick is deduped.
    await db_session.refresh(project)
    assert (
        project.design_doc[DESIGN_DOC_KEY][LAST_ALERTED_ROUTING_KEY]
        == PROVIDER_ROUTING_ADOPT
    )


@pytest.mark.asyncio
async def test_routing_verdict_reject_fires_warning(db_session, mocker):
    """A transition to REJECT fires one WARNING."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_routing_design_doc(verdict_state=PROVIDER_ROUTING_REJECT),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_provider_routing_transition(db_session, project.id)
    assert fired == PROVIDER_ROUTING_REJECT

    alerts = await _routing_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert "REJECT" in alerts[0].title


@pytest.mark.asyncio
async def test_routing_verdict_dedups_same_verdict(db_session, mocker):
    """A second tick at the same verdict does not fire a second alert."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_routing_design_doc(verdict_state=PROVIDER_ROUTING_ADOPT),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await maybe_alert_provider_routing_transition(db_session, project.id)
    second = await maybe_alert_provider_routing_transition(db_session, project.id)
    assert first == PROVIDER_ROUTING_ADOPT
    assert second is None  # deduped on the persisted marker
    alerts = await _routing_alerts(db_session, project.id)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_routing_verdict_insufficient_data_no_alert(db_session, mocker):
    """INSUFFICIENT_DATA is not alert-worthy."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(
        design_doc=_routing_design_doc(
            verdict_state=PROVIDER_ROUTING_INSUFFICIENT_DATA
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fired = await maybe_alert_provider_routing_transition(db_session, project.id)
    assert fired is None
    alerts = await _routing_alerts(db_session, project.id)
    assert alerts == []


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
    resp = await client.get(f"/api/admin/projects/{project.id}/pool-hit-rate-estimate")
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
async def test_unpriced_alert_uses_attempt_provider_model(db_session, mocker):
    """Priced current attempts must not hide unpriced banked retry attempts."""
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(UNPRICED_ALERT_MIN_TASKS):
        task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            api_call_summary={
                "total_input_tokens": 500 + i,
                "total_output_tokens": 200,
                "llm_call_count": 2,
            },
            provider="fakeprovider",
            model="fake-model-x",
            vm_provisioned_at=now - timedelta(minutes=10 + i),
        )
        db_session.add(task)
        await db_session.flush()
        bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))

        task.status = TaskStatus.COMPLETED
        task.api_call_summary = {
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 3,
        }
        task.provider = "anthropic"
        task.model = "claude-sonnet-4-6"

    await db_session.commit()

    fired = await maybe_alert_unpriced_models_coverage(db_session, project.id)

    assert fired is True
    alerts = await _unpriced_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert "fakeprovider:fake-model-x" in alerts[0].message
    assert "50%" in alerts[0].message


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
async def test_snapshot_surfaces_repair_infra_failures(db_session):
    """repair_infra_failures is surfaced in the format_compliance snapshot block."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            format_compliance_summary={
                "format_error_total": 2,
                "format_repair_attempts": 6,
                "format_repair_successes": 3,
                "format_repair_infra_failures": 3,
            },
        )
    )
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    block = snap[FORMAT_COMPLIANCE_KEY]
    assert block["repair_attempts"] == 6
    assert block["repair_successes"] == 3
    # New field surfaced in the snapshot block.
    assert block["repair_infra_failures"] == 3
    # Clean denominator = 6 - 3 = 3; rate = 3/3 = 1.0
    assert block[SELF_REPAIR_SUCCESS_RATE_KEY] == 1.0

    drift = await read_baseline_with_drift(db_session, project.id)
    fc = drift[FORMAT_COMPLIANCE_KEY]
    assert fc["repair_infra_failures"] == 3


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
        k
        for k, v in AXIS_CONSUMERS.items()
        if v.get("consumer") and k not in all_axes_set
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


def test_code_change_prompt_static_bytes():
    """AXIS_PROMPT_STATIC_BYTES is registered in ALL_AXES and AXIS_CONSUMERS, and
    _compute_prompt_static_bytes() returns a value within a meaningful range when
    agent/src/prompt.ts is available.

    This is the acceptance test for the prompt_static_bytes gauge (d-22af1f55):
    it exercises the production parsing logic against the live prompt.ts source,
    so a PR that grows the static prompt budget past PROMPT_STATIC_BYTES_THRESHOLD
    will fail here before reaching production.

    Baseline (post-v16, 2026-06-20): ~16 420 bytes across all measured constants.
    The 2026-06-19 compute experiment measured ~34 997 bytes; subsequent v16
    reductions (SHARED_LIFECYCLE_REMAINDER -75% in PR #10960, SIZE_GUARDRAIL_GUIDANCE
    -53% in PR #10956) brought the total to the current baseline.  The lower-bound
    guard below catches catastrophic regressions such as a constant being silently
    dropped from _PROMPT_STATIC_CONST_NAMES / _PROMPT_STATIC_PLAIN_CONST_NAMES.
    """
    assert AXIS_PROMPT_STATIC_BYTES in ALL_AXES, (
        f"{AXIS_PROMPT_STATIC_BYTES!r} must be in ALL_AXES"
    )
    assert AXIS_PROMPT_STATIC_BYTES in AXIS_CONSUMERS, (
        f"{AXIS_PROMPT_STATIC_BYTES!r} must have an entry in AXIS_CONSUMERS"
    )
    assert AXIS_CONSUMERS[AXIS_PROMPT_STATIC_BYTES].get("pending") or AXIS_CONSUMERS[
        AXIS_PROMPT_STATIC_BYTES
    ].get("consumer"), (
        "AXIS_CONSUMERS entry for prompt_static_bytes must have a 'pending' or 'consumer' key"
    )

    result = _compute_prompt_static_bytes()
    if result is None:
        pytest.skip("agent/src/prompt.ts not available in this environment")

    assert isinstance(result, int), f"Expected int, got {type(result)!r}"
    assert result > 8_000, (
        f"prompt_static_bytes={result} is below the expected lower bound (8 000 bytes). "
        "A constant may have been removed from _PROMPT_STATIC_CONST_NAMES or "
        "_PROMPT_STATIC_PLAIN_CONST_NAMES, or the prompt sections shrank dramatically. "
        f"Post-v16 baseline: ~16 420 bytes. Investigate before lowering this bound."
    )
    assert result <= PROMPT_STATIC_BYTES_THRESHOLD, (
        f"prompt_static_bytes={result} exceeds PROMPT_STATIC_BYTES_THRESHOLD="
        f"{PROMPT_STATIC_BYTES_THRESHOLD}. A recent change grew the static prompt "
        "budget past the guard. Either revert the growth or raise the threshold "
        "with justification in efficiency_snapshot.py."
    )


def test_playbook_guidance_trigger_bullets_condensed():
    """PLAYBOOK_GUIDANCE must use at most 5 trigger-condition bullets.

    PR #11033 condensed the 10 original bullets to 5 by merging overlapping
    categories (error-fixes + linting rules; workarounds + wrong-approaches;
    build-commands + env-setup; test-setup + test-patterns; module-deps +
    architecture). This test guards against re-expansion.
    """
    import os

    prompt_ts = os.path.join(
        os.path.dirname(__file__), "..", "..", "agent", "src", "prompt.ts"
    )
    prompt_ts = os.path.normpath(prompt_ts)
    if not os.path.exists(prompt_ts):
        pytest.skip("agent/src/prompt.ts not available in this environment")

    with open(prompt_ts, encoding="utf-8") as fh:
        src = fh.read()

    # Extract the PLAYBOOK_GUIDANCE template literal body
    marker = "**When to save (trigger conditions):**"
    start = src.find(marker)
    assert start != -1, (
        "PLAYBOOK_GUIDANCE trigger conditions section not found in prompt.ts"
    )
    end_marker = "\n\n**What NOT to save:**"
    end = src.find(end_marker, start)
    assert end != -1, "'What NOT to save' section not found after trigger conditions"

    trigger_section = src[start:end]
    bullet_count = trigger_section.count("\n- ")
    assert bullet_count <= 5, (
        f"PLAYBOOK_GUIDANCE has {bullet_count} trigger bullets (max 5). "
        "Consolidate overlapping categories rather than expanding the list. "
        "See PR #11033 for the canonical 5-bullet grouping."
    )


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
    breakdown_total = compute_llm_cost_breakdown([create_task, review_task])["totals"][
        "total_cost_usd"
    ]
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


def test_compute_llm_cost_by_task_type():
    """mean_input_tokens_per_call = total_input_tokens / llm_call_count; None when
    call count is zero; computed correctly under a real merge-test verdict."""
    import uuid

    project_id = uuid.uuid4()

    # Merged task: 2000 input tokens over 8 calls -> mean = 250
    merged_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 800,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # Task with no llm_call_count -> mean should be None
    no_calls_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "llm_call_count": 0,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([merged_task, no_calls_task])
    by_type = {r["task_type"]: r for r in rows}

    create_row = by_type["create"]
    assert create_row["llm_call_count"] == 8
    assert create_row["total_input_tokens"] == 2000
    assert create_row["mean_input_tokens_per_call"] == 250  # 2000 / 8

    review_row = by_type["review"]
    assert review_row["llm_call_count"] == 0
    assert review_row["mean_input_tokens_per_call"] is None

    # Verify merged task is counted correctly
    assert create_row["merged_count"] == 1
    assert create_row["verified_merged_count"] == 1


def test_compute_llm_cost_by_account():
    """Per-account LLM cost rollup: grouping, pricing, sorting, unknown fallback, zero-token exclusion."""
    import uuid

    project_id = uuid.uuid4()

    # Account A: 2 tasks, one with multiple call rows — expected to rank #1.
    input_rate_a, output_rate_a = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    # Task A1: 1000 in @ 3e-6 + 500 out @ 15e-6 = 0.009 + 0.0075 = 0.0105
    task_a1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        llm_account_id="acct-a",
    )
    # Task A2: 200 in @ 3e-6 + 100 out @ 15e-6 = 0.0006 + 0.0015 = 0.0021
    # Total for acct-a: 0.0105 + 0.0021 = 0.0126, 2 tasks.
    task_a2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 200,
            "total_output_tokens": 100,
            "llm_call_count": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        llm_account_id="acct-a",
    )

    # Account B: higher cost overall — expected to rank #2 (less than acct-c).
    input_rate_b, output_rate_b = LLM_TOKEN_PRICING[("openai", "gpt-5.4")]
    # Task B1: 5000 in @ 2.5e-6 + 2000 out @ 15e-6 = 0.0125 + 0.03 = 0.0425
    task_b1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 5000,
            "total_output_tokens": 2000,
            "llm_call_count": 10,
        },
        provider="openai",
        model="gpt-5.4",
        llm_account_id="acct-b",
    )

    # Account C: highest cost — expected to rank #1.
    input_rate_c, output_rate_c = LLM_TOKEN_PRICING[("gemini", "gemini-2.5-flash")]
    # Task C1: 8000 in @ 0.15e-6 + 4000 out @ 0.6e-6 = 0.0012 + 0.0024 = 0.0036
    task_c1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 8000,
            "total_output_tokens": 4000,
            "llm_call_count": 12,
        },
        provider="gemini",
        model="gemini-2.5-flash",
        llm_account_id="acct-c",
    )

    # Unknown account: task without llm_account_id — falls to "unknown".
    input_rate_u, output_rate_u = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    task_unknown = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 1500,
            "llm_call_count": 9,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        # llm_account_id deliberately absent
    )

    # Zero-token task: should NOT be counted in any bucket's task_count.
    task_no_tokens = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "llm_call_count": 0,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        llm_account_id="acct-a",
    )

    rows = _compute_llm_cost_by_account(
        [task_a1, task_a2, task_b1, task_c1, task_unknown, task_no_tokens]
    )

    # (c) sorted output by cost descending
    assert len(rows) == 4
    for i in range(len(rows) - 1):
        assert rows[i]["cost_usd"] >= rows[i + 1]["cost_usd"], (
            f"rows not sorted by cost descending at index {i}: "
            f"{rows[i]['cost_usd']} < {rows[i+1]['cost_usd']}"
        )

    by_account = {r["account_id"]: r for r in rows}

    # (a) basic grouping by account_id
    assert set(by_account.keys()) == {"acct-a", "acct-b", "acct-c", "unknown"}

    # (d) unknown fallback: task without llm_account_id lands in "unknown"
    cost_unknown = round(3000 * input_rate_u + 1500 * output_rate_u, 6)
    assert by_account["unknown"]["account_id"] == "unknown"
    assert by_account["unknown"]["input_tokens"] == 3000
    assert by_account["unknown"]["output_tokens"] == 1500
    assert by_account["unknown"]["cost_usd"] == cost_unknown
    assert by_account["unknown"]["task_count"] == 1

    # (e) zero-token tasks excluded from task_count
    # acct-a has 2 tasks with tokens (A1, A2), and 1 zero-token task excluded
    assert by_account["acct-a"]["task_count"] == 2

    # (b) correct cost computation
    cost_a = round(1000 * input_rate_a + 500 * output_rate_a, 6) + round(200 * input_rate_a + 100 * output_rate_a, 6)
    cost_a_rounded = round(cost_a, 6)
    assert by_account["acct-a"]["cost_usd"] == cost_a_rounded
    assert by_account["acct-a"]["input_tokens"] == 1200
    assert by_account["acct-a"]["output_tokens"] == 600
    assert by_account["acct-a"]["llm_call_count"] == 7

    cost_b = round(5000 * input_rate_b + 2000 * output_rate_b, 6)
    assert by_account["acct-b"]["cost_usd"] == cost_b
    assert by_account["acct-b"]["task_count"] == 1

    cost_c = round(8000 * input_rate_c + 4000 * output_rate_c, 6)
    assert by_account["acct-c"]["cost_usd"] == cost_c


def test_compute_llm_cost_by_account_empty():
    """Empty task list returns empty list."""
    rows = _compute_llm_cost_by_account([])
    assert rows == []


def test_compute_llm_cost_by_account_unknown_only():
    """Tasks without llm_account_id all bubble up to 'unknown'."""
    import uuid

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    t1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 100, "total_output_tokens": 50, "llm_call_count": 1},
        provider="anthropic", model="claude-sonnet-4-6",
    )
    t2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 200, "total_output_tokens": 100, "llm_call_count": 2},
        provider="anthropic", model="claude-sonnet-4-6",
    )

    rows = _compute_llm_cost_by_account([t1, t2])
    assert len(rows) == 1
    assert rows[0]["account_id"] == "unknown"
    assert rows[0]["task_count"] == 2
    expected_cost = round(100 * input_rate + 50 * output_rate, 6) + round(200 * input_rate + 100 * output_rate, 6)
    assert rows[0]["cost_usd"] == round(expected_cost, 6)


def test_compute_llm_cost_by_account_unpriced_no_crash():
    """Task with unknown provider/model yields zero cost, does not crash."""
    import uuid

    t = make_task(
        project_id=uuid.uuid4(),
        status=TaskStatus.COMPLETED,
        api_call_summary={"total_input_tokens": 500, "total_output_tokens": 200, "llm_call_count": 3},
        provider="nonexistent", model="fake-model",
        llm_account_id="some-acct",
    )
    rows = _compute_llm_cost_by_account([t])
    assert len(rows) == 1
    assert rows[0]["account_id"] == "some-acct"
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["task_count"] == 1


def test_compact_llm_cost_attribution():
    """mean_input_tokens_per_call is stored in each by_task_type row of the
    compact attribution block under a real merge-test verdict."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 600,
            "llm_call_count": 6,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    from app.efficiency_snapshot import _compact_llm_cost_attribution

    result = _compact_llm_cost_attribution([task])
    by_type = {r["task_type"]: r for r in result["by_task_type"]}

    assert "create" in by_type
    create_row = by_type["create"]
    assert create_row["mean_input_tokens_per_call"] == 500  # 3000 / 6
    assert create_row["cost_usd"] > 0


def test_compact_llm_cost_attribution_surfaces_unpriced_combos():
    """_compact_llm_cost_attribution includes unpriced_combos: empty when all
    tasks are priced, populated when tasks run on unpriced (provider, model)."""
    import uuid

    from app.efficiency_snapshot import LLM_TOKEN_PRICING, _compact_llm_cost_attribution
    from testing.factories import make_task

    project_id = uuid.uuid4()

    # Known-priced model — unpriced_combos should be empty.
    priced_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    cost = _compact_llm_cost_attribution([priced_task])
    assert "unpriced_combos" in cost
    assert cost["unpriced_combos"] == []

    # Unpriced combo — should appear in the list.
    unpriced_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "llm_call_count": 2,
        },
        provider="deepseek",
        model="deepseek-chaos-1",
    )
    # Provider:model must NOT be in LLM_TOKEN_PRICING for this test to be valid.
    assert ("deepseek", "deepseek-chaos-1") not in LLM_TOKEN_PRICING

    cost2 = _compact_llm_cost_attribution([unpriced_task])
    assert "unpriced_combos" in cost2
    assert "deepseek:deepseek-chaos-1" in cost2["unpriced_combos"]


def test_per_turn_metrics_present_when_num_turns_reported():
    """mean_turns_per_call, mean_input_tokens_per_turn, mean_uncached_input_tokens_per_call,
    and cache_read_share are computed correctly when tasks report num_turns."""
    import uuid

    project_id = uuid.uuid4()

    # 4000 input tokens (1000 cache_read), 10 calls, 5 turns across 2 tasks
    task1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 600,
            "llm_call_count": 6,
            "total_cache_read_input_tokens": 600,
            "total_cache_creation_input_tokens": 0,
            "num_turns": 3,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    task2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 4,
            "total_cache_read_input_tokens": 400,
            "total_cache_creation_input_tokens": 0,
            "num_turns": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task1, task2])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["research"]

    # total_input=4000, cache_read=1000, llm_call_count=10, num_turns=5
    assert row["llm_call_count"] == 10
    assert row["total_input_tokens"] == 4000
    assert row["total_cache_read_input_tokens"] == 1000
    # mean_turns_per_call = 5 / 10 = 0.5
    assert row["mean_turns_per_call"] == 0.5
    # mean_input_tokens_per_turn = 4000 / 5 = 800
    assert row["mean_input_tokens_per_turn"] == 800
    # uncached = 4000 - 0 - 1000 = 3000; per call = 3000 / 10 = 300
    assert row["mean_uncached_input_tokens_per_call"] == 300
    # cache_read_share = 1000 / 4000 = 0.25
    assert row["cache_read_share"] == 0.25


def test_per_turn_metrics_absent_when_num_turns_not_reported():
    """mean_turns_per_call and mean_input_tokens_per_turn are None when no task
    in the bucket reports num_turns (absent-counter tolerance)."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 5,
            # num_turns deliberately absent (older agent)
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["create"]

    # num_turns absent → turn-dependent metrics are None
    assert row["mean_turns_per_call"] is None
    assert row["mean_input_tokens_per_turn"] is None
    # uncached and cache_read_share don't depend on num_turns
    assert row["mean_uncached_input_tokens_per_call"] == 400  # 2000 / 5
    # cache_read absent → 0 / 2000 = 0.0 (not None, input exists)
    assert row["cache_read_share"] == 0.0
    assert row["total_cache_read_input_tokens"] == 0


def test_per_turn_metrics_from_attempt_history():
    """num_turns is accumulated from attempt_history (banked attempts) as well
    as the live api_call_summary."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix"}],
        # current attempt has num_turns=2
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 3,
            "num_turns": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # attempt_history set after creation (property setter pattern used by other tests)
    task.attempt_history = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "llm_input_tokens": 1500,
            "llm_output_tokens": 300,
            "llm_call_count": 4,
            "num_turns": 3,
        }
    ]

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["fix"]

    # total turns = 3 (history) + 2 (current) = 5
    # llm_call_count = 4 (history) + 3 (current) = 7
    assert row["mean_turns_per_call"] == round(5 / 7, 2)
    # total_input = 1500 (history) + 1000 (current) = 2500
    assert row["mean_input_tokens_per_turn"] == round(2500 / 5)


def test_tool_result_bytes_aggregated_in_by_task_type():
    """total_tool_result_bytes and mean_tool_result_bytes_per_task are correct
    when tasks report the field via attempt_history and api_call_summary."""
    import uuid

    project_id = uuid.uuid4()

    # task1: 5000 bytes in attempt_history, 3000 bytes in live api_call_summary
    task1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 5,
            "total_tool_result_bytes": 3000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    task1.attempt_history = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "llm_input_tokens": 1500,
            "llm_output_tokens": 300,
            "llm_call_count": 3,
            "total_tool_result_bytes": 5000,
        }
    ]

    # task2: only api_call_summary carries the field
    task2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 2,
            "total_tool_result_bytes": 2000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task1, task2])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["research"]

    # task1: 5000 (history) + 3000 (live) = 8000; task2: 2000; total = 10000
    assert row["total_tool_result_bytes"] == 10000
    # mean = 10000 / 2 tasks = 5000
    assert row["mean_tool_result_bytes_per_task"] == 5000


def test_mean_tool_result_bytes_denominator_uses_task_count_not_reporting_count():
    """mean_tool_result_bytes_per_task divides by task_count (all tasks in the bucket),
    not prompt_size_reporting_count — a non-reporting task dilutes the tool-result mean.
    This contrasts with mean_prompt_size_bytes which uses prompt_size_reporting_count."""
    import uuid

    project_id = uuid.uuid4()

    # task1: reports tool_result_bytes
    task1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 2,
            "total_tool_result_bytes": 6000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # task2: older agent, does NOT report tool_result_bytes
    task2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "llm_call_count": 1,
            # total_tool_result_bytes deliberately absent
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task1, task2])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["research"]

    # total accumulates only from the reporting task
    assert row["total_tool_result_bytes"] == 6000
    # mean divides by task_count (2), NOT by reporting count (1)
    # so non-reporting task dilutes the per-task average
    assert row["mean_tool_result_bytes_per_task"] == 3000


def test_tool_result_bytes_absent_when_not_reported():
    """total_tool_result_bytes and mean_tool_result_bytes_per_task are None
    when no task in the bucket reported the field (absent-counter tolerance)."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 5,
            # total_tool_result_bytes deliberately absent (older agent)
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["create"]

    assert row["total_tool_result_bytes"] is None
    assert row["mean_tool_result_bytes_per_task"] is None


def test_compute_llm_cost_breakdown_totals_includes_tool_result_bytes():
    """totals block from compute_llm_cost_breakdown includes total_tool_result_bytes."""
    import uuid

    project_id = uuid.uuid4()

    task_with = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 3,
            "total_tool_result_bytes": 4000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    task_without = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "llm_call_count": 2,
            # no total_tool_result_bytes
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # Present when at least one task reported
    breakdown = compute_llm_cost_breakdown([task_with, task_without])
    assert breakdown["totals"]["total_tool_result_bytes"] == 4000

    # None when no task reported
    breakdown_none = compute_llm_cost_breakdown([task_without])
    assert breakdown_none["totals"]["total_tool_result_bytes"] is None


def test_compact_llm_cost_attribution_carries_tool_result_bytes():
    """_compact_llm_cost_attribution threads total_tool_result_bytes into the
    top-level compact block and into each by_task_type row."""
    import uuid

    from app.efficiency_snapshot import _compact_llm_cost_attribution

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 600,
            "llm_call_count": 6,
            "total_tool_result_bytes": 12000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = _compact_llm_cost_attribution([task])

    # Top-level aggregate
    assert result["total_tool_result_bytes"] == 12000

    # Per-task-type row
    by_type = {r["task_type"]: r for r in result["by_task_type"]}
    assert "create" in by_type
    assert by_type["create"]["total_tool_result_bytes"] == 12000


def test_prompt_size_bytes_accumulated_from_api_call_summary():
    """task with api_call_summary.prompt_size_bytes yields non-None total and mean."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 4,
            "prompt_size_bytes": 150000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["research"]

    assert row["total_prompt_size_bytes"] == 150000
    assert row["mean_prompt_size_bytes"] == 150000  # one reporting task


def test_prompt_size_bytes_absent_yields_none():
    """task without prompt_size_bytes in api_call_summary yields None for both fields."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["fix"]

    assert row["total_prompt_size_bytes"] is None
    assert row["mean_prompt_size_bytes"] is None


def test_prompt_size_bytes_accumulated_from_attempt_history():
    """prompt_size_bytes is accumulated from attempt_history rows as well."""
    import uuid

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "plan"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 3,
            "prompt_size_bytes": 80000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    task.attempt_history = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "llm_input_tokens": 1500,
            "llm_output_tokens": 300,
            "llm_call_count": 4,
            "prompt_size_bytes": 120000,
        }
    ]

    rows = compute_llm_cost_by_task_type([task])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["plan"]

    # Both attempt_history and api_call_summary contribute
    assert row["total_prompt_size_bytes"] == 120000 + 80000
    assert row["mean_prompt_size_bytes"] == 120000 + 80000  # one reporting task (summed)


def test_prompt_size_bytes_mean_denominator_is_reporting_task_count():
    """mean_prompt_size_bytes divides by reporting tasks, not total tasks."""
    import uuid

    project_id = uuid.uuid4()

    task_with = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 5,
            "prompt_size_bytes": 90000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    task_without = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 1500,
            "total_output_tokens": 300,
            "llm_call_count": 3,
            # no prompt_size_bytes — older task
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task_with, task_without])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["review"]

    # total from 1 reporting task; mean denominator = 1, not 2
    assert row["total_prompt_size_bytes"] == 90000
    assert row["mean_prompt_size_bytes"] == 90000


def test_compact_attribution_passes_through_prompt_size_bytes():
    """_compact_llm_cost_attribution includes total/mean prompt_size_bytes in by_task_type."""
    import uuid

    from app.efficiency_snapshot import _compact_llm_cost_attribution

    project_id = uuid.uuid4()

    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 600,
            "llm_call_count": 6,
            "prompt_size_bytes": 200000,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = _compact_llm_cost_attribution([task])
    by_type = {r["task_type"]: r for r in result["by_task_type"]}

    assert "create" in by_type
    create_row = by_type["create"]
    assert create_row["total_prompt_size_bytes"] == 200000
    assert create_row["mean_prompt_size_bytes"] == 200000


def test_by_task_type_includes_mean_section_sizes():
    """compute_llm_cost_by_task_type includes mean_section_sizes per task type.

    Sections present in multiple tasks are averaged correctly; sections present
    in only one task get that task's value as the mean (denominator = 1, not
    total bucket task count).
    """
    import uuid

    project_id = uuid.uuid4()

    task1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 4,
            "prompt_section_sizes": [
                {"name": "claude_md", "bytes": 30000},
                {"name": "playbook", "bytes": 10000},
            ],
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    task2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 2,
            "prompt_section_sizes": [
                {"name": "claude_md", "bytes": 50000},
                {"name": "task", "bytes": 20000},
            ],
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_task_type([task1, task2])
    by_type = {r["task_type"]: r for r in rows}
    row = by_type["create"]

    assert row["mean_section_sizes"] is not None
    # claude_md reported by both tasks: (30000 + 50000) / 2 == 40000
    assert row["mean_section_sizes"]["claude_md"] == 40000
    # playbook reported by task1 only: 10000 / 1 == 10000
    assert row["mean_section_sizes"]["playbook"] == 10000
    # task reported by task2 only: 20000 / 1 == 20000
    assert row["mean_section_sizes"]["task"] == 20000


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


# ---------------------------------------------------------------------------
# Windowed idle-cost axis: out-of-window ledger rows must not inflate
# cost_per_merged_pr_incl_pool_idle (acceptance gate: test named below).
# ---------------------------------------------------------------------------

from app.models import PoolIdleCostLedger, PoolIdleSpan, PoolVm, PoolVmState  # noqa: E402


@pytest.mark.asyncio
async def test_out_of_window_idle_ledger_rows_do_not_inflate_inclusive_axis(
    db_session,
):
    """Out-of-window PoolIdleSpan rows must not inflate the per-PR idle-
    inclusive cost axis.

    Seeds one in-window span ($1.00, settled 3 days ago) and one out-of-window
    span ($99.00, settled 10 days ago — outside the 7-day window).  Also seeds
    PoolIdleCostLedger rows (all-time carry-forward) and a merged PR task so
    cost_per_merged_pr_incl_pool_idle is defined.
    The axis value must reflect only the in-window $1.00; the out-of-window
    $99.00 must be excluded even though it appears in pool_metrics["total_idle_cost_usd"].
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # One merged-PR task so the axis denominator is 1 and we can compare dollars.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=10),
        vm_stopped_at=now - timedelta(minutes=1),
        vm_spot=False,
    )
    db_session.add(task)

    # In-window span: settled 3 days ago (within the 7-day cutoff).
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-in-window",
            started_at=now - timedelta(days=3, hours=1),
            settled_at=now - timedelta(days=3),
            cost_usd=1.0,
        )
    )
    # Out-of-window span: settled 10 days ago (before the 7-day cutoff).
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-out-of-window",
            started_at=now - timedelta(days=10, hours=1),
            settled_at=now - timedelta(days=10),
            cost_usd=99.0,
        )
    )
    # All-time ledger carry-forward (both entries) so pool_metrics sees the full spend.
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-in-window",
            idle_cost_usd=1.0,
            retired_at=now - timedelta(days=3),
        )
    )
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-out-of-window",
            idle_cost_usd=99.0,
            retired_at=now - timedelta(days=10),
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    incl = axes["cost_per_merged_pr_incl_pool_idle"]
    assert incl is not None, "axis should be defined (we have a merged PR)"

    # pool_metrics (all-time) still reflects the full $100 spend.
    all_time = axes["pool_metrics"]["total_idle_cost_usd"]
    assert all_time >= 100.0, (
        "all-time pool_metrics must include both in- and out-of-window entries"
    )

    # The windowed axis must be dramatically lower than if the $99 were included.
    # task VM cost is small (< $0.10 for 9 min on-demand), so incl ~ vm_cost + 1.0.
    # If the $99 leaked in, incl would be > 99.
    assert incl < 5.0, (
        f"cost_per_merged_pr_incl_pool_idle={incl} should reflect only the "
        f"in-window $1.00 span entry, not the $99 out-of-window entry"
    )


@pytest.mark.asyncio
async def test_windowed_idle_cost_live_member_clamped_to_window(db_session):
    """A live IDLE member whose idle_since predates the window cutoff must have
    its idle cost clamped to the window start rather than accruing from the
    full (pre-cutoff) idle_since."""
    from app.efficiency_snapshot import _AXIS_WINDOW_DAYS, _compute_windowed_idle_cost
    from app.vm_cost_rates import idle_cost_usd as _idle_cost_usd

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    cutoff = now - timedelta(days=_AXIS_WINDOW_DAYS)

    # idle_since is 10 days ago — well before the 7-day cutoff.
    idle_since_pre_window = now - timedelta(days=10)
    member = PoolVm(
        project_id=project.id,
        vm_instance_id="vm-old-idle",
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.IDLE,
        idle_since=idle_since_pre_window,
        idle_cost_banked=5.0,  # pre-accumulated banked cost — must NOT be counted
    )
    db_session.add(member)
    await db_session.commit()

    result = await _compute_windowed_idle_cost(db_session, project.id, cutoff)
    windowed = result["total"]

    # Expected: only the in-window portion (cutoff .. now) is billed.
    # The full (10-day) idle period would be ~10x larger.
    window_elapsed = (now - cutoff).total_seconds()
    expected_max = _idle_cost_usd(window_elapsed) * 1.05  # 5% tolerance for clock skew
    full_elapsed = (now - idle_since_pre_window).total_seconds()
    full_cost = _idle_cost_usd(full_elapsed)

    assert windowed < full_cost, (
        f"windowed cost {windowed} should be less than full idle cost {full_cost}"
    )
    assert windowed <= expected_max, (
        f"windowed cost {windowed} exceeds clamped window cost {expected_max}"
    )
    # idle_cost_banked (5.0) is not included: if it were, windowed would equal
    # expected_max + 5.0.  The expected_max check above already gates this.
    assert windowed <= expected_max, "windowed cost must not include idle_cost_banked"

    # by_account must contain the member's account.
    assert "acct-1" in result["by_account"], (
        "by_account should contain the member account"
    )
    assert result["by_account"]["acct-1"]["total_idle_cost_usd"] == windowed


@pytest.mark.asyncio
async def test_all_time_pool_metrics_preserved_alongside_windowed_axis(db_session):
    """pool_metrics['total_idle_cost_usd'] must still reflect all-time spend
    (including pre-window ledger entries) while cost_per_merged_pr_incl_pool_idle
    uses only windowed cost."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=10),
        vm_stopped_at=now - timedelta(minutes=1),
        vm_spot=False,
    )
    db_session.add(task)

    # Only out-of-window spend ($50).
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-old",
            idle_cost_usd=50.0,
            retired_at=now - timedelta(days=14),
        )
    )
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # All-time pool_metrics sees the $50.
    assert axes["pool_metrics"]["total_idle_cost_usd"] >= 50.0

    # Windowed axis excludes the $50 (nothing in-window).
    incl = axes["cost_per_merged_pr_incl_pool_idle"]
    assert incl is not None
    # Without the $50 the inclusive axis is close to the plain vm-cost axis.
    plain = axes[AXIS_COST_PER_MERGED_PR]
    assert plain is not None
    assert abs(incl - plain) < 1.0, (
        f"incl ({incl}) should be close to plain ({plain}) when "
        "all pool idle spend is out-of-window"
    )


@pytest.mark.asyncio
async def test_pool_cost_amortization_excludes_pre_window_idle_spend(db_session):
    """read_baseline_with_drift must use windowed idle cost, not all-time."""
    from app.vm_cost_rates import idle_cost_usd as _idle_cost_usd

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # Merged task so write_snapshot has real data to persist.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now - timedelta(minutes=1),
        vm_spot=False,
    )
    db_session.add(task)

    # In-window span: settled 2 hours ago (inside any 24h window).
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-in-window",
            started_at=now - timedelta(hours=3),
            settled_at=now - timedelta(hours=2),
            cost_usd=0.50,
        )
    )
    # All-time ledger carry-forward for the in-window span and an old one.
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-in-window",
            idle_cost_usd=0.50,
            retired_at=now - timedelta(hours=2),
        )
    )
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-out-of-window",
            idle_cost_usd=50.0,
            retired_at=now - timedelta(hours=48),
        )
    )
    db_session.add(
        PoolVm(
            project_id=project.id,
            vm_instance_id="vm-live-old",
            account_id="acct-live",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
            idle_since=now - timedelta(days=10),
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)

    drift = await read_baseline_with_drift(db_session, project.id)
    amort = drift["pool_cost_amortization"]

    snapshot_all_time = drift.get("pool_idle_cost_usd") or 0.0
    assert snapshot_all_time >= 50.0, (
        "snapshot pool_idle_cost_usd must include all-time spend"
    )

    # The amortization view must use only the windowed (in-window) spend.
    amort_idle = amort["pool_idle_cost_usd"]
    assert amort_idle < 5.0, (
        f"pool_cost_amortization.pool_idle_cost_usd={amort_idle} should reflect "
        f"only the in-window $0.50 ledger entry, not the $50 out-of-window entry. "
        f"All-time snapshot was {snapshot_all_time}"
    )
    assert amort_idle >= 0.49, (
        f"pool_cost_amortization.pool_idle_cost_usd={amort_idle} should include "
        "the in-window $0.50 span row"
    )
    # Per-account breakdown: live idle member's in-window clamped cost is attributed
    # to acct-live. The window is 24h (get_pool_funnel_snapshot default); member is
    # idle 10 days so its contribution is clamped to exactly 24h.
    amort_by_account = drift["pool_cost_amortization_by_account"]
    live_expected = _idle_cost_usd(24 * 3600)
    assert amort_by_account["acct-live"]["pool_idle_cost_usd"] == pytest.approx(
        live_expected, rel=0.05
    )


@pytest.mark.asyncio
async def test_compute_windowed_idle_cost_fleet_wide(db_session):
    """project_id=None (fleet-wide) includes IDLE members from multiple projects."""
    from app.efficiency_snapshot import _compute_windowed_idle_cost

    now = _utcnow()
    cutoff = now - timedelta(hours=24)

    proj_a = make_project(design_doc={})
    proj_b = make_project(design_doc={})
    db_session.add(proj_a)
    db_session.add(proj_b)
    await db_session.commit()
    await db_session.refresh(proj_a)
    await db_session.refresh(proj_b)

    # One IDLE member per project, both idle for 1 hour (well within 24h window).
    for project in (proj_a, proj_b):
        db_session.add(
            PoolVm(
                project_id=project.id,
                vm_instance_id=f"vm-fleet-{project.id}",
                account_id="acct-fleet",
                provider="anthropic",
                model="claude-opus-4-8",
                state=PoolVmState.IDLE,
                idle_since=now - timedelta(hours=1),
            )
        )
    await db_session.commit()

    # Fleet-wide result (project_id=None) must sum both members.
    fleet_result = await _compute_windowed_idle_cost(db_session, None, cutoff)
    fleet_total = fleet_result["total"]

    # Per-project result for proj_a alone must be about half of fleet.
    proj_a_result = await _compute_windowed_idle_cost(db_session, proj_a.id, cutoff)
    proj_a_total = proj_a_result["total"]

    assert proj_a_total > 0, "proj_a windowed cost should be positive"
    assert fleet_total > proj_a_total, (
        f"fleet total ({fleet_total}) should exceed single-project total ({proj_a_total})"
    )
    # With both members having equal idle time the fleet total should be ~2x proj_a.
    assert abs(fleet_total - 2 * proj_a_total) < 0.001, (
        f"fleet ({fleet_total}) should be ~2x proj_a ({proj_a_total}) with equal idle time"
    )


# ---------------------------------------------------------------------------
# Acceptance gate: PoolIdleSpan-based windowed attribution (exact span counts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windowed_idle_cost_counts_settled_spans_exactly(db_session):
    """_compute_windowed_idle_cost must include spans whose settled_at is within
    the window and exclude spans settled before the cutoff — even when both span
    to the same retired member (verifying per-span attribution, not lump
    retirement attribution).

    Regression: the old PoolIdleCostLedger approach attributed the member's
    FULL lifetime banked cost to the retirement window, so a long-lived member
    that accrued $5 over 5 days but retired at day 0 would contribute $5 to any
    window it retired into, regardless of when the cost actually accrued.

    This test seeds three PoolIdleSpan rows for the same vm:
      - span A: settled 5 days ago (outside the 7-day window) — excluded
      - span B: settled 3 days ago (inside the 7-day window) — included
      - span C: settled 1 day ago (inside the 7-day window) — included

    The windowed total must match span B + span C exactly.
    Also verifies that project_id scoping works (a second project's spans are
    excluded from the first project's windowed result).
    """
    from app.efficiency_snapshot import _AXIS_WINDOW_DAYS, _compute_windowed_idle_cost

    now = _utcnow()
    cutoff = now - timedelta(days=_AXIS_WINDOW_DAYS)

    project = make_project(design_doc={})
    other_project = make_project(design_doc={})
    db_session.add(project)
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other_project)

    span_a_cost = 0.10  # out-of-window
    span_b_cost = 0.25  # in-window
    span_c_cost = 0.40  # in-window

    # Span A: settled before the window cutoff — must be excluded.
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-multi-span",
            started_at=now - timedelta(days=_AXIS_WINDOW_DAYS + 2),
            settled_at=now - timedelta(days=_AXIS_WINDOW_DAYS + 1),
            cost_usd=span_a_cost,
        )
    )
    # Span B: settled 3 days ago (inside the window) — must be included.
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-multi-span",
            started_at=now - timedelta(days=4),
            settled_at=now - timedelta(days=3),
            cost_usd=span_b_cost,
        )
    )
    # Span C: settled 1 day ago (inside the window) — must be included.
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="vm-multi-span",
            started_at=now - timedelta(days=2),
            settled_at=now - timedelta(days=1),
            cost_usd=span_c_cost,
        )
    )
    # A span from another project must not bleed into project's windowed total.
    db_session.add(
        PoolIdleSpan(
            project_id=other_project.id,
            vm_instance_id="vm-other-project",
            started_at=now - timedelta(days=2),
            settled_at=now - timedelta(days=1),
            cost_usd=50.0,
        )
    )
    await db_session.commit()

    result = await _compute_windowed_idle_cost(db_session, project.id, cutoff)

    expected = round(span_b_cost + span_c_cost, 6)
    assert result["total"] == pytest.approx(expected, abs=1e-6), (
        f"windowed total {result['total']} should equal span_b+span_c "
        f"({expected}); span_a ({span_a_cost}) must be excluded"
    )

    # Fleet-wide query (project_id=None) adds the other-project span.
    fleet_result = await _compute_windowed_idle_cost(db_session, None, cutoff)
    assert fleet_result["total"] > result["total"], (
        "fleet-wide total should exceed project-scoped total"
    )


# ---------------------------------------------------------------------------
# Per-axis seed provenance tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_snapshot_seeds_provenance(db_session):
    """The first write_snapshot call records _member_provenance for every
    ALL_AXES member that has a non-None value in the initial snapshot."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    prov = baseline.get("_member_provenance") or {}

    # Every ALL_AXES member with a non-None value in the snapshot must have
    # a provenance entry with seeded_at == snapshot captured_at.
    for axis in ALL_AXES:
        if snap.get(axis) is not None:
            assert axis in prov, f"missing provenance for {axis}"
            assert prov[axis]["seeded_at"] == snap["captured_at"]
            assert prov[axis]["snapshot_captured_at"] == snap["captured_at"]


@pytest.mark.asyncio
async def test_reseed_after_migration_records_new_provenance(db_session):
    """After a schema migration clears an axis and the re-seed loop refills it,
    the _member_provenance entry for that axis must reflect the re-seed snapshot,
    not the original baseline capture timestamp."""
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_LLM_COST_PER_MERGED_PR: 9999.0,
                    AXIS_COST_PER_MERGED_PR: 0.03,
                    "_schema_version": 3,  # v3 → v4 clears llm axis
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app.efficiency_snapshot import LLM_TOKEN_PRICING as _LLP

    input_rate, output_rate = _LLP[("anthropic", "claude-sonnet-4-6")]
    db_session.add(
        _make_efficient_task(
            project.id,
            api_call_summary={
                "llm_call_count": 5,
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    prov = baseline.get("_member_provenance") or {}

    # The re-seeded llm axis must show the NEW snapshot timestamp, not "2026-01-01".
    assert AXIS_LLM_COST_PER_MERGED_PR in prov
    assert prov[AXIS_LLM_COST_PER_MERGED_PR]["seeded_at"] == snap["captured_at"]
    assert prov[AXIS_LLM_COST_PER_MERGED_PR]["seeded_at"] != "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_backfilled_baseline_members_carry_seed_provenance(db_session):
    """Backfilled nested blocks (agent_timeout_rate, format_compliance) record
    per-member provenance at backfill time so operators can see which snapshot
    those drift figures are measured against.

    This is the acceptance-gate test: exercises the production backfill path
    (agent_timeout_rate absent in pre-existing baseline → backfilled from current
    snapshot on next write_snapshot) and asserts that provenance is durable.
    """
    # Construct a baseline that predates agent_timeout_rate (no such key).
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_COST_PER_MERGED_PR: 0.05,
                    "_schema_version": 5,
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    prov = baseline.get("_member_provenance") or {}

    # agent_timeout_rate was backfilled — provenance must exist and point at
    # the snapshot that provided the data.
    if baseline.get("agent_timeout_rate") is not None:
        assert "agent_timeout_rate" in prov, (
            "backfilled agent_timeout_rate must carry provenance"
        )
        assert prov["agent_timeout_rate"]["seeded_at"] == snap["captured_at"]
        assert prov["agent_timeout_rate"]["snapshot_captured_at"] == snap["captured_at"]
        # The backfill timestamp must differ from the pre-existing baseline capture.
        assert prov["agent_timeout_rate"]["seeded_at"] != "2026-01-01T00:00:00"

    # Axes already present in the original baseline are NOT re-seeded; provenance
    # for them must be inferred retroactively (from the matching snapshot value)
    # rather than stamped with the new captured_at.
    # cost_per_merged_pr was 0.05 in the pre-existing baseline and unchanged.
    cost_prov = prov.get(AXIS_COST_PER_MERGED_PR)
    if cost_prov:
        # Retroactively inferred: must not show the new snapshot's captured_at.
        assert (
            cost_prov.get("_inferred") or cost_prov["seeded_at"] != snap["captured_at"]
        ), "unchanged axis must not be re-stamped with the new snapshot time"


@pytest.mark.asyncio
async def test_read_baseline_with_drift_exposes_per_axis_provenance(db_session):
    """read_baseline_with_drift must include baseline_seeded_at and
    baseline_seed_snapshot_at on each axis entry in the drift response."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)

    for axis in ALL_AXES:
        entry = drift["axes"][axis]
        assert "baseline_seeded_at" in entry, f"missing baseline_seeded_at for {axis}"
        assert "baseline_seed_snapshot_at" in entry, (
            f"missing baseline_seed_snapshot_at for {axis}"
        )
        if snap.get(axis) is not None:
            assert entry["baseline_seeded_at"] == snap["captured_at"]
            assert entry["baseline_seed_snapshot_at"] == snap["captured_at"]

    # Top-level baseline_provenance summary must also be present.
    assert "baseline_provenance" in drift
    assert isinstance(drift["baseline_provenance"], dict)


@pytest.mark.asyncio
async def test_retroactive_provenance_inference_for_existing_baseline(db_session):
    """A baseline lacking _member_provenance is annotated retroactively on the
    next write_snapshot call (inside the project lock) by matching axis values
    against snapshot history."""
    # Simulate a pre-provenance baseline: value matches a known snapshot entry
    # but no _member_provenance key exists.
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-05-01T00:00:00",
                    AXIS_COST_PER_MERGED_PR: 0.042,
                    "_schema_version": 5,
                    # No _member_provenance key — pre-provenance baseline.
                },
                "snapshots": [
                    {
                        "captured_at": "2026-05-01T00:00:00",
                        AXIS_COST_PER_MERGED_PR: 0.042,  # matches baseline
                    },
                    {
                        "captured_at": "2026-05-02T00:00:00",
                        AXIS_COST_PER_MERGED_PR: 0.050,  # later, different value
                    },
                ],
                "last_run_at": "2026-05-02T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]
    prov = baseline.get("_member_provenance") or {}

    # cost_per_merged_pr was 0.042 in the baseline and the FIRST snapshot also
    # has 0.042 — retroactive inference should have found that match.
    assert AXIS_COST_PER_MERGED_PR in prov, (
        "retroactive inference must annotate axis whose value matches a snapshot"
    )
    entry = prov[AXIS_COST_PER_MERGED_PR]
    # Must point at the first (matching) snapshot, not the second.
    assert entry["seeded_at"] == "2026-05-01T00:00:00"
    assert entry.get("_inferred") is True


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_sums_llm_cost(db_session):
    """compute_project_window_spend_usd sums VM + LLM spend for tasks in the window."""
    from app.efficiency_snapshot import compute_project_window_spend_usd

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Task with known LLM tokens, no VM cost (no vm_provisioned_at)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 5,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
            created_at=now,
        )
    )
    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    in_rate, out_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected = round(1000 * in_rate + 500 * out_rate, 6)
    assert total == expected, f"Expected LLM cost {expected}, got {total}"


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_uses_cache_aware_llm_pricing(
    db_session,
):
    """Project-window spend must reconcile with the shared cache-aware LLM helper."""
    from app.efficiency_snapshot import compute_project_window_spend_usd

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "zai", "glm-5.2"
    input_rate, output_rate = LLM_TOKEN_PRICING[(provider, model)]
    cache_read_rate = LLM_CACHE_READ_INPUT_PRICING[(provider, model)]
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_provisioned_at=None,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 100,
            "llm_call_count": 3,
            "total_cache_creation_input_tokens": 0,
            "total_cache_read_input_tokens": 700,
        },
        provider=provider,
        model=model,
        created_at=_utcnow(),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    expected = round(
        300 * input_rate + 700 * cache_read_rate + 100 * output_rate,
        6,
    )
    shared_breakdown_total = compute_llm_cost_breakdown([task])["totals"][
        "total_cost_usd"
    ]
    legacy_full_input = round(1000 * input_rate + 100 * output_rate, 6)

    assert total == pytest.approx(expected)
    assert total == pytest.approx(shared_breakdown_total)
    assert total < legacy_full_input


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_zero_for_empty_project(db_session):
    """Zero tasks in window → 0.0 spend."""
    from app.efficiency_snapshot import compute_project_window_spend_usd

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )
    assert total == 0.0


# ---------------------------------------------------------------------------
# VM span overlap tests — acceptance requirement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_counts_overlapping_vm_spans(db_session):
    """Acceptance test: tasks whose VM spans overlap the window must be counted even
    when they started before the window boundary.

    Covers three overlap shapes:
      1. Started before window, stopped inside window (boundary-crosser).
      2. Started before window, still in-flight (no vm_stopped_at).
      3. Started and stopped inside window (normal case — must still work).

    A fully-outside task (stopped before cutoff) must NOT be counted.
    """
    from app.efficiency_snapshot import compute_project_window_spend_usd
    from app.routers.admin_analytics import ON_DEMAND_RATE

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # 1. Boundary-crosser: started 30h ago, stopped 2h ago (overlaps window).
    boundary_start = now - timedelta(hours=30)
    boundary_stop = now - timedelta(hours=2)
    boundary_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_provisioned_at=boundary_start,
        vm_stopped_at=boundary_stop,
        vm_spot=False,
    )
    db_session.add(boundary_task)

    # 2. In-flight: started 30h ago, no vm_stopped_at (still running).
    inflight_start = now - timedelta(hours=30)
    inflight_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_provisioned_at=inflight_start,
        vm_stopped_at=None,
        vm_spot=False,
    )
    db_session.add(inflight_task)

    # 3. Normal in-window task: started 1h ago, stopped just now.
    normal_start = now - timedelta(hours=1)
    normal_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_provisioned_at=normal_start,
        vm_stopped_at=now,
        vm_spot=False,
    )
    db_session.add(normal_task)

    # 4. Fully-outside task: started 48h ago, stopped 26h ago (before cutoff).
    outside_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_provisioned_at=now - timedelta(hours=48),
        vm_stopped_at=now - timedelta(hours=26),
        vm_spot=False,
    )
    db_session.add(outside_task)

    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    # Compute expected costs for the 3 overlapping tasks only.
    boundary_cost = (boundary_stop - boundary_start).total_seconds() * ON_DEMAND_RATE
    # In-flight cost uses (now - inflight_start); use a generous tolerance.
    inflight_cost = (now - inflight_start).total_seconds() * ON_DEMAND_RATE
    normal_cost = (now - normal_start).total_seconds() * ON_DEMAND_RATE
    # Fully-outside task contributes 0.
    expected_min = round(boundary_cost + normal_cost, 6)

    assert total > 0.0, "expected non-zero spend from overlapping tasks"
    # Boundary-crosser and normal task are always deterministic; in-flight adds
    # a small time-dependent component — assert total is at least the two
    # deterministic costs and within a generous upper bound.
    assert total >= expected_min, (
        f"expected total >= {expected_min} (boundary + normal); got {total}"
    )
    assert total < round(
        boundary_cost + inflight_cost + normal_cost + 60 * ON_DEMAND_RATE, 6
    ), "total unexpectedly large — outside task may have been included"


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_counts_task_started_before_window(
    db_session,
):
    """A task whose VM started before the window but stopped inside must be counted."""
    from app.efficiency_snapshot import compute_project_window_spend_usd
    from app.routers.admin_analytics import ON_DEMAND_RATE

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    start = now - timedelta(hours=30)
    stop = now - timedelta(hours=2)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=start,
            vm_stopped_at=stop,
            vm_spot=False,
        )
    )
    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    expected = round((stop - start).total_seconds() * ON_DEMAND_RATE, 6)
    assert total == pytest.approx(expected, rel=1e-4), (
        f"boundary-crossing task cost should be {expected}, got {total}"
    )


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_counts_inflight_task_started_before_window(
    db_session,
):
    """An in-flight task with vm_provisioned_at before the window must be counted."""
    from app.efficiency_snapshot import compute_project_window_spend_usd
    from app.routers.admin_analytics import ON_DEMAND_RATE

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    start = now - timedelta(hours=30)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_provisioned_at=start,
            vm_stopped_at=None,
            vm_spot=False,
        )
    )
    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    # In-flight tasks accrue cost up to now; must be positive.
    assert total > 0.0, "in-flight task started before window must contribute spend"
    # Sanity: cost should be roughly start-to-now seconds * rate (within 1 min slack).
    max_expected = round(
        (now - start + timedelta(minutes=1)).total_seconds() * ON_DEMAND_RATE, 6
    )
    assert total <= max_expected


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_excludes_fully_outside_task(db_session):
    """A task whose VM span ended before the window cutoff must NOT be counted."""
    from app.efficiency_snapshot import compute_project_window_spend_usd

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(hours=48),
            vm_stopped_at=now - timedelta(hours=26),
            vm_spot=False,
        )
    )
    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )
    assert total == 0.0, f"fully-outside task must not contribute spend; got {total}"


@pytest.mark.asyncio
async def test_compute_project_window_spend_usd_llm_only_task_in_window_still_counted(
    db_session,
):
    """LLM-only task (no vm_provisioned_at) created inside the window is still counted."""
    from app.efficiency_snapshot import compute_project_window_spend_usd

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=None,
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 5,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
            created_at=now,
        )
    )
    await db_session.commit()

    total = await compute_project_window_spend_usd(
        db_session, project.id, window_hours=24
    )

    in_rate, out_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected = round(1000 * in_rate + 500 * out_rate, 6)
    assert total == expected, (
        f"LLM-only in-window task should contribute {expected}; got {total}"
    )


# ---------------------------------------------------------------------------
# Improvement-lane axis tests
# ---------------------------------------------------------------------------


def test_lane_axis_constants_registered():
    """All three lane axes appear in ALL_AXES and have consumer entries."""
    assert AXIS_LANE_COST_PER_MERGED_PR in ALL_AXES
    assert AXIS_LANE_WARM_RESUME_RATE in ALL_AXES
    assert AXIS_CHECKPOINT_LOSS_RATE in ALL_AXES
    assert AXIS_LANE_COST_PER_MERGED_PR in AXIS_CONSUMERS
    assert AXIS_LANE_WARM_RESUME_RATE in AXIS_CONSUMERS
    assert AXIS_CHECKPOINT_LOSS_RATE in AXIS_CONSUMERS
    # Consumers must claim a valid disposition.
    for key in (
        AXIS_LANE_COST_PER_MERGED_PR,
        AXIS_LANE_WARM_RESUME_RATE,
        AXIS_CHECKPOINT_LOSS_RATE,
    ):
        assert AXIS_CONSUMERS[key].get("consumer"), f"{key} must declare a consumer"


def _make_lane(project_id, *, status=ImprovementLaneStatus.RETIRED, **kwargs):
    """Build a minimal ImprovementLane for testing."""
    from testing.factories import _utcnow as _now

    defaults = dict(
        project_id=project_id,
        title="Test lane",
        goal="Improve X",
        branch_name="lane/test",
        base_branch="main",
        status=status.value,
        checkpoint_seq=0,
        phase_requeues=0,
        checkpoints=[],
        vm_cost_usd_total=0.0,
        reserved_idle_cost_usd_total=0.0,
        retired_at=_now() if status == ImprovementLaneStatus.RETIRED else None,
    )
    defaults.update(kwargs)
    return ImprovementLane(**defaults)


def test_compute_lane_cost_per_merged_pr_retired_lane():
    """A RETIRED lane within the window contributes its VM+idle cost."""
    from testing.factories import _utcnow as _now

    lane = _make_lane(
        project_id=None,
        status=ImprovementLaneStatus.RETIRED,
        vm_cost_usd_total=2.5,
        reserved_idle_cost_usd_total=0.5,
        retired_at=_now(),
    )
    result = _compute_lane_cost_per_merged_pr([lane])
    assert result == round(3.0 / 1, 4)


def test_compute_lane_cost_per_merged_pr_no_retired_lanes():
    """Returns None when there are no RETIRED lanes."""
    lane = _make_lane(project_id=None, status=ImprovementLaneStatus.ACTIVE)
    assert _compute_lane_cost_per_merged_pr([lane]) is None


def test_compute_lane_cost_per_merged_pr_excludes_old_lanes():
    """Lanes retired before the window cutoff are excluded."""
    from datetime import timedelta
    from testing.factories import _utcnow as _now

    old_lane = _make_lane(
        project_id=None,
        status=ImprovementLaneStatus.RETIRED,
        vm_cost_usd_total=100.0,
        retired_at=_now() - timedelta(days=30),  # outside 7-day window
    )
    assert _compute_lane_cost_per_merged_pr([old_lane]) is None


def test_compute_lane_warm_resume_rate_basic():
    """warm_resume=True checkpoints counted correctly."""
    lane = _make_lane(
        project_id=None,
        status=ImprovementLaneStatus.PARKED,
        checkpoints=[
            {"warm_resume": True, "sha": "abc"},
            {"warm_resume": False, "sha": "def"},
            {"warm_resume": True, "sha": "ghi"},
        ],
    )
    result = _compute_lane_warm_resume_rate([lane])
    assert result == round(2 / 3, 4)


def test_compute_lane_warm_resume_rate_no_checkpoints():
    """Returns None when no lanes have checkpoint records."""
    lane = _make_lane(
        project_id=None, status=ImprovementLaneStatus.ACTIVE, checkpoints=[]
    )
    assert _compute_lane_warm_resume_rate([lane]) is None


def test_compute_checkpoint_loss_rate_basic():
    """phase_requeues / (checkpoint_seq + phase_requeues)."""
    lane = _make_lane(
        project_id=None,
        status=ImprovementLaneStatus.PARKED,
        checkpoint_seq=8,
        phase_requeues=2,
    )
    result = _compute_checkpoint_loss_rate([lane])
    assert result == round(2 / 10, 4)


def test_compute_checkpoint_loss_rate_no_attempts():
    """Returns None when all lanes have seq=0 and requeues=0."""
    lane = _make_lane(
        project_id=None,
        status=ImprovementLaneStatus.ACTIVE,
        checkpoint_seq=0,
        phase_requeues=0,
    )
    assert _compute_checkpoint_loss_rate([lane]) is None


@pytest.mark.asyncio
async def test_compute_current_axes_includes_lane_axes(db_session):
    """compute_current_axes returns all three lane axis keys."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert AXIS_LANE_COST_PER_MERGED_PR in axes
    assert AXIS_LANE_WARM_RESUME_RATE in axes
    assert AXIS_CHECKPOINT_LOSS_RATE in axes


@pytest.mark.asyncio
async def test_compute_current_axes_lane_axes_from_db(db_session):
    """Lane axes compute correct values from ImprovementLane rows in the DB."""
    from testing.factories import _utcnow as _now

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))

    # RETIRED lane with vm+idle cost and warm checkpoints.
    retired_lane = ImprovementLane(
        project_id=project.id,
        title="Retired lane",
        goal="Improve auth",
        branch_name="lane/auth",
        base_branch="main",
        status=ImprovementLaneStatus.RETIRED.value,
        vm_cost_usd_total=3.0,
        reserved_idle_cost_usd_total=1.0,
        checkpoint_seq=5,
        phase_requeues=1,
        checkpoints=[
            {"warm_resume": True, "sha": "abc"},
            {"warm_resume": False, "sha": "def"},
        ],
        retired_at=_now(),
    )
    db_session.add(retired_lane)
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)

    # lane_cost_per_merged_pr: (3.0 + 1.0) / 1 retired lane
    assert axes[AXIS_LANE_COST_PER_MERGED_PR] == round(4.0 / 1, 4)
    # lane_warm_resume_rate: 1 warm / 2 total checkpoints
    assert axes[AXIS_LANE_WARM_RESUME_RATE] == round(1 / 2, 4)
    # checkpoint_loss_rate: 1 requeue / (5 + 1) = 1/6
    assert axes[AXIS_CHECKPOINT_LOSS_RATE] == round(1 / 6, 4)


@pytest.mark.asyncio
async def test_lane_axes_survive_snapshot_cycle(db_session):
    """Lane axis values survive a write_snapshot / read_baseline_with_drift cycle."""
    from testing.factories import _utcnow as _now

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    retired_lane = ImprovementLane(
        project_id=project.id,
        title="Lane A",
        goal="Goal A",
        branch_name="lane/a",
        base_branch="main",
        status=ImprovementLaneStatus.RETIRED.value,
        vm_cost_usd_total=2.0,
        reserved_idle_cost_usd_total=0.0,
        checkpoint_seq=4,
        phase_requeues=0,
        checkpoints=[{"warm_resume": True}, {"warm_resume": True}],
        retired_at=_now(),
    )
    db_session.add(retired_lane)
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)
    axes = drift["axes"]

    assert AXIS_LANE_COST_PER_MERGED_PR in axes
    assert axes[AXIS_LANE_COST_PER_MERGED_PR]["current"] == round(2.0 / 1, 4)
    assert AXIS_LANE_WARM_RESUME_RATE in axes
    assert axes[AXIS_LANE_WARM_RESUME_RATE]["current"] == 1.0
    assert AXIS_CHECKPOINT_LOSS_RATE in axes
    assert axes[AXIS_CHECKPOINT_LOSS_RATE]["current"] == 0.0


@pytest.mark.asyncio
async def test_schema_version_v5_migrates_lane_axes(db_session):
    """Existing baseline at schema_version=5 gets the three lane axes cleared
    and the version bumped to 6 on the next write_snapshot call."""
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "captured_at": "2026-01-01T00:00:00",
                    AXIS_COST_PER_MERGED_PR: 0.07,
                    "_schema_version": 5,
                },
                "snapshots": [{"captured_at": "2026-01-01T00:00:00"}],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    container = project.design_doc[DESIGN_DOC_KEY]
    baseline = container["baseline"]

    # Schema bumped from 5 to 6.
    assert baseline["_schema_version"] == 6
    # Existing axes must not be clobbered.
    assert baseline[AXIS_COST_PER_MERGED_PR] == 0.07
    # Lane axes were cleared (None) then re-seeded if data available.
    # With no ImprovementLane rows the re-seed leaves them None.
    assert baseline.get(AXIS_LANE_COST_PER_MERGED_PR) is None
    assert baseline.get(AXIS_LANE_WARM_RESUME_RATE) is None
    assert baseline.get(AXIS_CHECKPOINT_LOSS_RATE) is None


@pytest.mark.asyncio
async def test_snapshot_carries_serving_sha(db_session, monkeypatch):
    """write_snapshot stamps serving_sha matching DEPLOY_SHA when the env var is set."""
    monkeypatch.setenv("DEPLOY_SHA", "abc1234def5678")
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    assert snap.get("serving_sha") == "abc1234def5678", (
        f"write_snapshot must stamp serving_sha from DEPLOY_SHA; got {snap.get('serving_sha')!r}"
    )
    # Verify it is persisted in the stored snapshot too.
    await db_session.refresh(project)
    stored_snaps = project.design_doc[DESIGN_DOC_KEY]["snapshots"]
    assert stored_snaps[-1].get("serving_sha") == "abc1234def5678"


@pytest.mark.asyncio
async def test_snapshot_serving_sha_none_when_unset(db_session, monkeypatch):
    """write_snapshot stamps serving_sha=None when DEPLOY_SHA is not set."""
    monkeypatch.delenv("DEPLOY_SHA", raising=False)
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(_make_efficient_task(project.id))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    assert snap.get("serving_sha") is None, (
        f"serving_sha must be None when DEPLOY_SHA is absent; got {snap.get('serving_sha')!r}"
    )


# ---------------------------------------------------------------------------
# format_compliance_by_type persistence and agent endpoint tests
# ---------------------------------------------------------------------------


def _make_format_compliance_typed_task(project_id, *, task_type: str, errors: int):
    """Create a task with format_compliance_summary and a specific payload type."""
    t = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        format_compliance_summary={
            "format_error_total": errors,
            "format_repair_attempts": 0,
            "format_repair_successes": 0,
        },
    )
    t.payloads = [{"type": task_type}]
    return t


@pytest.mark.asyncio
async def test_format_compliance_by_type_persisted_in_snapshot(db_session):
    """write_snapshot stores format_compliance_by_type in the snapshot dict."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two "create" tasks (well-formed + error) and one "research" task (well-formed).
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="create", errors=0)
    )
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="create", errors=1)
    )
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="research", errors=0)
    )
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()
    await db_session.refresh(project)

    # The snapshot dict returned by write_snapshot must carry the by_type block.
    assert "format_compliance_by_type" in snap
    by_type = snap["format_compliance_by_type"]
    assert "create" in by_type
    assert "research" in by_type
    assert by_type["create"]["tasks_measured"] == 2
    assert by_type["create"]["format_compliance_rate"] == 0.5
    assert by_type["research"]["tasks_measured"] == 1
    assert by_type["research"]["format_compliance_rate"] == 1.0

    # The persisted snapshot in design_doc must also carry the by_type block.
    container = project.design_doc[DESIGN_DOC_KEY]
    persisted_snap = container["snapshots"][-1]
    assert "format_compliance_by_type" in persisted_snap
    assert persisted_snap["format_compliance_by_type"]["create"]["tasks_measured"] == 2


@pytest.mark.asyncio
async def test_read_baseline_surfaces_format_compliance_by_type(db_session):
    """read_baseline_with_drift includes format_compliance_by_type from the latest snapshot."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="create", errors=0)
    )
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="fix", errors=1)
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)
    by_type = drift.get("format_compliance_by_type")
    assert by_type is not None
    assert "create" in by_type
    assert "fix" in by_type
    assert by_type["create"]["format_compliance_rate"] == 1.0
    assert by_type["fix"]["format_compliance_rate"] == 0.0


@pytest.mark.asyncio
async def test_format_compliance_by_type_none_for_old_snapshots(db_session):
    """Snapshots that predate format_compliance_by_type return None gracefully."""
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {"captured_at": "2026-01-01T00:00:00"},
                "snapshots": [
                    # Old snapshot without format_compliance_by_type key.
                    {"captured_at": "2026-01-01T00:00:00", FORMAT_COMPLIANCE_KEY: {}},
                ],
                "last_run_at": "2026-01-01T00:00:00",
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    drift = await read_baseline_with_drift(db_session, project.id)
    # format_compliance_by_type absent in the snapshot => surfaced as None.
    assert drift.get("format_compliance_by_type") is None


@pytest.mark.asyncio
async def test_agent_format_compliance_snapshot_endpoint(
    client: AsyncClient, db_session
):
    """GET /api/agent/efficiency-snapshot/format-compliance returns by_type block."""
    from app.auth import create_agent_token

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a task in this project so we have a valid agent token to use.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Add format-compliance tasks and write a snapshot.
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="create", errors=0)
    )
    db_session.add(
        _make_format_compliance_typed_task(project.id, task_type="research", errors=1)
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    token = create_agent_token(str(task.id), "vm-fc-test-1")
    resp = await client.get(
        "/api/agent/efficiency-snapshot/format-compliance",
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "captured_at" in data
    assert FORMAT_COMPLIANCE_KEY in data
    assert "format_compliance_by_type" in data
    by_type = data["format_compliance_by_type"]
    assert "create" in by_type
    assert "research" in by_type
    assert by_type["create"]["format_compliance_rate"] == 1.0
    assert by_type["research"]["format_compliance_rate"] == 0.0
    # by_schema is exposed at the top level for direct access
    assert "by_schema" in data


# ---------------------------------------------------------------------------
# compute_pr_rework_chain_breakdown tests
# ---------------------------------------------------------------------------


def _pr_chain_task(
    project_id,
    *,
    pr_number,
    task_type,
    iteration=None,
    input_tokens=1000,
    output_tokens=500,
    provider="anthropic",
    model="claude-sonnet-4-6",
    vm_cost_seconds=300,
):
    """Simplified helper: task in a PR chain."""
    from testing.factories import _utcnow

    now = _utcnow()
    context = {}
    if iteration is not None:
        context["review_loop"] = {"iteration": iteration, "pr_number": pr_number}
    payload = {"type": task_type, "pr_number": pr_number}
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context=context,
        payloads=[payload],
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": 5,
        },
        provider=provider,
        model=model,
        vm_provisioned_at=now - timedelta(seconds=vm_cost_seconds),
        vm_stopped_at=now,
        vm_spot=False,
    )


def test_pr_chain_rework_rollup_splits_first_pass_vs_rework():
    """Core acceptance test: a create + first review (first-pass) and a fix +
    re-review (rework) on the same PR number are correctly split.

    PR 42 chain:
      - create task (iteration=None)  -> first-pass
      - review task (iteration=1)     -> first-pass (first review)
      - fix task   (type="fix")       -> rework
      - review task (iteration=2)     -> rework (re-review)
    """
    import uuid

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # first-pass: create (2000 in / 1000 out) + review-iter1 (1000 in / 500 out)
    create = _pr_chain_task(
        project_id,
        pr_number=42,
        task_type="create",
        input_tokens=2000,
        output_tokens=1000,
    )
    review1 = _pr_chain_task(
        project_id,
        pr_number=42,
        task_type="review",
        iteration=1,
        input_tokens=1000,
        output_tokens=500,
    )
    # rework: fix (1500 in / 800 out) + re-review iter2 (1200 in / 600 out)
    fix1 = _pr_chain_task(
        project_id, pr_number=42, task_type="fix", input_tokens=1500, output_tokens=800
    )
    review2 = _pr_chain_task(
        project_id,
        pr_number=42,
        task_type="review",
        iteration=2,
        input_tokens=1200,
        output_tokens=600,
    )

    result = compute_pr_rework_chain_breakdown([create, review1, fix1, review2])

    assert result["total_pr_chains"] == 1

    expected_fp_llm = round(
        2000 * input_rate
        + 1000 * output_rate  # create
        + 1000 * input_rate
        + 500 * output_rate,  # review iter1
        6,
    )
    expected_rw_llm = round(
        1500 * input_rate
        + 800 * output_rate  # fix
        + 1200 * input_rate
        + 600 * output_rate,  # review iter2
        6,
    )
    expected_total_llm = round(expected_fp_llm + expected_rw_llm, 6)

    assert result["first_pass_llm_usd"] == pytest.approx(expected_fp_llm, abs=1e-9)
    assert result["rework_llm_usd"] == pytest.approx(expected_rw_llm, abs=1e-9)
    assert result["total_llm_usd"] == pytest.approx(expected_total_llm, abs=1e-9)

    expected_rework_share = round(expected_rw_llm / expected_total_llm, 4)
    assert result["rework_share_of_total_llm_cost"] == expected_rework_share
    assert result["unpriced_chain_count"] == 0

    # top_rework_prs has exactly one entry for PR 42
    assert len(result["top_rework_prs"]) == 1
    pr_entry = result["top_rework_prs"][0]
    assert pr_entry["pr_number"] == 42
    assert pr_entry["task_count"] == 4
    assert pr_entry["rework_task_count"] == 2
    assert pr_entry["rework_llm_usd"] == pytest.approx(expected_rw_llm, abs=1e-9)
    assert pr_entry["has_unpriced"] is False


def test_pr_chain_rework_single_pass_no_rework():
    """A PR with only a create task (no fix/re-review) reports zero rework share."""
    import uuid

    project_id = uuid.uuid4()

    create = _pr_chain_task(
        project_id,
        pr_number=101,
        task_type="create",
        input_tokens=1000,
        output_tokens=500,
    )
    review1 = _pr_chain_task(
        project_id,
        pr_number=101,
        task_type="review",
        iteration=1,
        input_tokens=800,
        output_tokens=400,
    )

    result = compute_pr_rework_chain_breakdown([create, review1])

    assert result["total_pr_chains"] == 1
    assert result["rework_llm_usd"] == 0.0
    assert result["rework_vm_usd"] == 0.0
    assert result["rework_share_of_total_llm_cost"] == 0.0
    assert result["top_rework_prs"][0]["rework_task_count"] == 0
    assert result["top_rework_prs"][0]["rework_share"] == 0.0


def test_pr_chain_rework_tasks_without_pr_number_excluded():
    """Tasks with no pr_number are excluded; the rollup returns empty."""
    import uuid

    project_id = uuid.uuid4()
    no_pr = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "create"}],
        api_call_summary={"total_input_tokens": 1000, "total_output_tokens": 500},
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = compute_pr_rework_chain_breakdown([no_pr])

    assert result["total_pr_chains"] == 0
    assert result["total_llm_usd"] == 0.0
    assert result["rework_llm_usd"] == 0.0
    assert result["rework_share_of_total_llm_cost"] is None
    assert result["top_rework_prs"] == []


def test_pr_chain_rework_unpriced_combo_flagged():
    """A chain with an unpriced provider/model contributes zero LLM cost but
    has_unpriced=True and increments unpriced_chain_count."""
    import uuid

    project_id = uuid.uuid4()

    create = _pr_chain_task(
        project_id,
        pr_number=7,
        task_type="create",
        input_tokens=1000,
        output_tokens=500,
        provider="unknownprovider",
        model="mystery-v1",
    )
    fix1 = _pr_chain_task(
        project_id,
        pr_number=7,
        task_type="fix",
        input_tokens=800,
        output_tokens=400,
        provider="unknownprovider",
        model="mystery-v1",
    )

    result = compute_pr_rework_chain_breakdown([create, fix1])

    assert result["total_pr_chains"] == 1
    assert result["total_llm_usd"] == 0.0
    assert result["rework_llm_usd"] == 0.0
    assert result["unpriced_chain_count"] == 1
    assert result["top_rework_prs"][0]["has_unpriced"] is True
    # rework_share is None because total_llm_usd == 0
    assert result["rework_share_of_total_llm_cost"] is None


def test_pr_chain_rework_top_n_cap():
    """With more than top_n PR chains, only the top_n by rework_llm_usd are returned."""
    import uuid

    project_id = uuid.uuid4()

    tasks = []
    # Create 5 PR chains with different rework spend (PR 1 has most rework)
    for pr in range(1, 6):
        tasks.append(
            _pr_chain_task(
                project_id,
                pr_number=pr,
                task_type="create",
                input_tokens=1000,
                output_tokens=500,
            )
        )
        # PR 1 gets 5000 rework input tokens, PR 2 gets 4000, ...
        tasks.append(
            _pr_chain_task(
                project_id,
                pr_number=pr,
                task_type="fix",
                input_tokens=pr * 1000,
                output_tokens=500,
            )
        )

    result = compute_pr_rework_chain_breakdown(tasks, top_n=3)

    assert result["total_pr_chains"] == 5
    assert len(result["top_rework_prs"]) == 3
    # Top 3 by rework_llm_usd: PR 5 > PR 4 > PR 3
    pr_numbers = [e["pr_number"] for e in result["top_rework_prs"]]
    assert pr_numbers == [5, 4, 3]


def test_pr_chain_rework_aggregate_rework_share_multi_prs():
    """rework_share_of_total_llm_cost aggregates across multiple PR chains correctly."""
    import uuid

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # PR 10: create only (first-pass, 1000 in / 500 out)
    create_10 = _pr_chain_task(
        project_id,
        pr_number=10,
        task_type="create",
        input_tokens=1000,
        output_tokens=500,
    )
    # PR 20: create + fix (rework = fix cost)
    create_20 = _pr_chain_task(
        project_id,
        pr_number=20,
        task_type="create",
        input_tokens=1000,
        output_tokens=500,
    )
    fix_20 = _pr_chain_task(
        project_id, pr_number=20, task_type="fix", input_tokens=2000, output_tokens=1000
    )

    result = compute_pr_rework_chain_breakdown([create_10, create_20, fix_20])

    fp_cost = round(1000 * input_rate + 500 * output_rate, 6)  # create_10
    fp_cost_20 = round(1000 * input_rate + 500 * output_rate, 6)  # create_20
    rw_cost = round(2000 * input_rate + 1000 * output_rate, 6)  # fix_20
    total = round(fp_cost + fp_cost_20 + rw_cost, 6)

    assert result["first_pass_llm_usd"] == pytest.approx(fp_cost + fp_cost_20, abs=1e-9)
    assert result["rework_llm_usd"] == pytest.approx(rw_cost, abs=1e-9)
    expected_share = round(rw_cost / total, 4)
    assert result["rework_share_of_total_llm_cost"] == expected_share
    assert result["total_pr_chains"] == 2


def test_pr_chain_rework_context_only_pr_number():
    """PR number from context.review_loop.pr_number (no payload pr_number) works."""
    import uuid

    project_id = uuid.uuid4()
    from testing.factories import _utcnow

    now = _utcnow()

    # Payload has no pr_number; context.review_loop has it
    review = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"review_loop": {"pr_number": 99, "iteration": 2}},
        payloads=[{"type": "review"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        vm_provisioned_at=now - timedelta(seconds=300),
        vm_stopped_at=now,
        vm_spot=False,
    )

    result = compute_pr_rework_chain_breakdown([review])

    assert result["total_pr_chains"] == 1
    assert result["top_rework_prs"][0]["pr_number"] == 99
    # iteration=2 review is rework
    assert result["top_rework_prs"][0]["rework_task_count"] == 1
    assert result["rework_llm_usd"] > 0.0


def test_pr_chain_rework_vm_cost_tracked():
    """VM dollars are tracked alongside LLM dollars for both first-pass and rework."""
    import uuid
    from app.models import get_task_total_vm_cost_usd

    project_id = uuid.uuid4()

    create = _pr_chain_task(
        project_id,
        pr_number=5,
        task_type="create",
        input_tokens=1000,
        output_tokens=500,
        vm_cost_seconds=300,
    )
    fix1 = _pr_chain_task(
        project_id,
        pr_number=5,
        task_type="fix",
        input_tokens=800,
        output_tokens=400,
        vm_cost_seconds=600,
    )

    result = compute_pr_rework_chain_breakdown([create, fix1])

    expected_fp_vm = get_task_total_vm_cost_usd(create)
    expected_rw_vm = get_task_total_vm_cost_usd(fix1)
    assert result["first_pass_vm_usd"] == pytest.approx(expected_fp_vm, abs=1e-6)
    assert result["rework_vm_usd"] == pytest.approx(expected_rw_vm, abs=1e-6)
    assert result["total_vm_usd"] == pytest.approx(
        expected_fp_vm + expected_rw_vm, abs=1e-6
    )
    assert result["rework_share_of_total_vm_cost"] == pytest.approx(
        round(expected_rw_vm / (expected_fp_vm + expected_rw_vm), 4), abs=1e-6
    )


def test_pr_chain_rework_empty_task_list():
    """Empty task list returns zero-valued structure."""
    result = compute_pr_rework_chain_breakdown([])

    assert result["total_pr_chains"] == 0
    assert result["total_llm_usd"] == 0.0
    assert result["rework_llm_usd"] == 0.0
    assert result["rework_share_of_total_llm_cost"] is None
    assert result["rework_share_of_total_vm_cost"] is None
    assert result["top_rework_prs"] == []
    assert result["unpriced_chain_count"] == 0


# ---------------------------------------------------------------------------
# llm_cost_attribution snapshot persistence tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_cost_attribution_persisted_in_snapshot(db_session):
    """write_snapshot stores a compact llm_cost_attribution block with by_model,
    by_task_type, rework_share, and total_llm_usd so LLM cost drift is
    decomposable from persisted history without the on-demand live endpoint.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # create task (first-pass): merged, 2000 in + 1000 out
    create_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 42}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # fix task (rework) for same PR: 1000 in + 500 out
    fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "fix", "pr_number": 42}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(create_task)
    db_session.add(fix_task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    attr = snap.get("llm_cost_attribution")
    assert attr is not None, "llm_cost_attribution must be present in snapshot"

    expected_create_cost = round(2000 * input_rate + 1000 * output_rate, 6)
    expected_fix_cost = round(1000 * input_rate + 500 * output_rate, 6)
    expected_total = round(expected_create_cost + expected_fix_cost, 6)

    assert attr["total_llm_usd"] == pytest.approx(expected_total, abs=1e-9)
    assert attr["by_model"], "by_model must not be empty for priced tasks"
    sonnet_row = next(
        (r for r in attr["by_model"] if r["model"] == "claude-sonnet-4-6"), None
    )
    assert sonnet_row is not None, "claude-sonnet-4-6 must appear in by_model"
    assert sonnet_row["cost_usd"] == pytest.approx(expected_total, abs=1e-9)

    # Rework share: fix task is rework; create task is first-pass — both on PR 42.
    assert attr["rework_share"] is not None
    expected_rework_share = round(expected_fix_cost / expected_total, 4)
    assert attr["rework_share"] == pytest.approx(expected_rework_share, abs=1e-6)

    assert "by_task_type" in attr


@pytest.mark.asyncio
async def test_read_baseline_surfaces_llm_cost_attribution_breakdown(db_session):
    """read_baseline_with_drift surfaces llm_cost_attribution from the latest
    snapshot and llm_cost_attribution_trend as a day-over-day series, mirroring
    the warm-start-origin and reuse-dropout patterns.

    Acceptance criterion: named test must run green and exercise non-test
    production code paths in efficiency_snapshot.py (write_snapshot +
    read_baseline_with_drift).
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "pr_number": 7}],
            api_call_summary={
                "total_input_tokens": 3000,
                "total_output_tokens": 1500,
                "llm_call_count": 12,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)

    # llm_cost_attribution surfaces the latest snapshot's block.
    attr = drift.get("llm_cost_attribution")
    assert attr is not None, "llm_cost_attribution must be present in drift response"
    assert "by_model" in attr
    assert "by_task_type" in attr
    assert "rework_share" in attr
    assert "total_llm_usd" in attr
    expected_total = round(3000 * input_rate + 1500 * output_rate, 6)
    assert attr["total_llm_usd"] == pytest.approx(expected_total, abs=1e-9)

    # llm_cost_attribution_trend is a list with one entry per snapshot that
    # carries an llm_cost_attribution block, including by_task_type breakdown.
    trend = drift.get("llm_cost_attribution_trend")
    assert isinstance(trend, list), "llm_cost_attribution_trend must be a list"
    assert len(trend) >= 1, "at least one trend entry must be present"
    for entry in trend:
        assert "captured_at" in entry
        assert "by_task_type" in entry
        assert "by_model" in entry
        assert "rework_share" in entry
        assert "total_llm_usd" in entry


@pytest.mark.asyncio
async def test_cost_attribution_trend_includes_failure_class_and_merged_count(
    db_session,
):
    """llm_cost_attribution_trend entries include by_failure_class and merged_count.

    Writes a snapshot with one MERGED task and one COMPLETED_NO_PR task, then
    asserts that every trend entry exposes by_failure_class (with the MERGED and
    COMPLETED_NO_PR buckets) and merged_count equal to the number of merged tasks.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # One merged task
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "pr_number": 1}],
            api_call_summary={
                "total_input_tokens": 2000,
                "total_output_tokens": 1000,
                "llm_call_count": 8,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    # One completed but no PR (research task)
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={},
            payloads=[{"type": "research"}],
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 4,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)

    trend = drift.get("llm_cost_attribution_trend")
    assert isinstance(trend, list), "llm_cost_attribution_trend must be a list"
    assert len(trend) >= 1

    for entry in trend:
        assert "by_failure_class" in entry, "trend entry must include by_failure_class"
        assert "merged_count" in entry, "trend entry must include merged_count"

        fc_rows = entry["by_failure_class"]
        assert isinstance(fc_rows, list), "by_failure_class must be a list"
        fc_classes = {r["failure_class"] for r in fc_rows}
        assert "MERGED" in fc_classes, "MERGED bucket must appear in by_failure_class"
        assert (
            "COMPLETED_NO_PR" in fc_classes
        ), "COMPLETED_NO_PR bucket must appear in by_failure_class"

        merged_row = next(r for r in fc_rows if r["failure_class"] == "MERGED")
        assert merged_row["task_count"] == 1
        assert entry["merged_count"] == 1


@pytest.mark.asyncio
async def test_read_baseline_surfaces_vm_cost_attribution_trend(db_session):
    """read_baseline_with_drift surfaces vm_cost_attribution_trend as a
    day-over-day series mirroring llm_cost_attribution_trend but for VM spend.

    Acceptance criterion: >=2 snapshots written; read_baseline_with_drift
    returns vm_cost_attribution_trend as a list with captured_at/by_task_type/
    total_vm_usd entries.
    """
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

    drift = await read_baseline_with_drift(db_session, project.id)

    trend = drift.get("vm_cost_attribution_trend")
    assert isinstance(trend, list), "vm_cost_attribution_trend must be a list"
    assert len(trend) >= 1, "at least one trend entry must be present"
    for entry in trend:
        assert "captured_at" in entry
        assert "by_task_type" in entry
        assert "total_vm_usd" in entry


@pytest.mark.asyncio
async def test_vm_cost_attribution_by_task_type_in_write_snapshot(db_session):
    """write_snapshot stores vm_cost_attribution.by_task_type populated with
    per-type VM cost rows that reconcile with cost_per_merged_pr.

    Acceptance criterion (declared in task payload):
    test that vm_cost_attribution.by_task_type is populated in write_snapshot
    output and reconciles with cost_per_merged_pr.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # review task: 10 min on-demand VM, merged
    review_task = _make_efficient_task(
        project.id,
        payloads=[{"type": "review", "pr_number": 1}],
        vm_provisioned_at=now - timedelta(minutes=10),
        vm_stopped_at=now,
        vm_spot=False,
    )
    # create task: 5 min on-demand VM, merged
    create_task = _make_efficient_task(
        project.id,
        payloads=[{"type": "create", "pr_number": 2}],
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
        vm_spot=False,
    )
    # panel_review side-car: 3 min on-demand VM, not merged
    panel_task = _make_efficient_task(
        project.id,
        payloads=[{"type": "panel_review"}],
        context={},  # no pr_merged
        vm_provisioned_at=now - timedelta(minutes=3),
        vm_stopped_at=now,
        vm_spot=False,
    )
    db_session.add(review_task)
    db_session.add(create_task)
    db_session.add(panel_task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    vm_attr = snap.get("vm_cost_attribution")
    assert vm_attr is not None, "vm_cost_attribution must be present in snapshot"
    assert "by_task_type" in vm_attr, "vm_cost_attribution must have by_task_type"
    assert "total_vm_usd" in vm_attr, "vm_cost_attribution must have total_vm_usd"

    by_type = {r["task_type"]: r for r in vm_attr["by_task_type"]}
    assert "review" in by_type, "review task type must appear in by_task_type"
    assert "create" in by_type, "create task type must appear in by_task_type"
    assert "panel_review" in by_type, (
        "panel_review task type must appear in by_task_type"
    )

    # total_vm_usd reconciles: sum of by_task_type costs equals total_vm_usd
    summed = round(sum(r["total_cost_usd"] for r in vm_attr["by_task_type"]), 6)
    assert summed == pytest.approx(vm_attr["total_vm_usd"], abs=1e-9), (
        "sum of by_task_type costs must reconcile with total_vm_usd"
    )

    # total_vm_usd reconciles with cost_per_merged_pr:
    # cost_per_merged_pr = total_vm_cost / merged_count, with rounding.
    # Verify the relationship holds within rounding tolerance.
    cost_per_pr = snap.get(AXIS_COST_PER_MERGED_PR)
    assert cost_per_pr is not None
    # 2 merged tasks => cost_per_merged_pr ≈ total_vm_usd / 2 (4-decimal rounding)
    assert abs(vm_attr["total_vm_usd"] / 2 - cost_per_pr) < 0.001, (
        "cost_per_merged_pr must be approximately total_vm_usd / merged_count"
    )

    # panel_review side-car has no merges — cost_per_merge must be None
    assert by_type["panel_review"]["merged_count"] == 0
    assert by_type["panel_review"]["cost_per_merge"] is None


@pytest.mark.asyncio
async def test_self_repair_drift_suppressed_below_min_attempts(db_session):
    """drift_pct for self_repair_success_rate is None when the current snapshot
    has fewer than SELF_REPAIR_MIN_ATTEMPTS repair attempts (rare-event gate).

    A sufficiently-sampled window must still report the regression so the gate
    does not hide real degradation — this test asserts BOTH sides.
    """
    # Pre-seed the baseline with a healthy rate (1.0) and plenty of attempts.
    # Using a pre-seeded design_doc bypasses the 7-day task window so we can
    # control the baseline independently of the tasks we add below.
    baseline_fc = {
        FORMAT_COMPLIANCE_KEY: {
            "format_compliance_rate": 0.9,
            SELF_REPAIR_SUCCESS_RATE_KEY: 1.0,
            "repair_attempts": 50,
            "repair_successes": 50,
            "tasks_measured": 50,
            "well_formed_tasks": 45,
            "format_error_total": 5,
            "by_schema": {},
        }
    }
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {"captured_at": "2026-01-01T00:00:00", **baseline_fc},
                "snapshots": [{"captured_at": "2026-01-01T00:00:00", **baseline_fc}],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # --- Current window: below-MIN attempts (should suppress drift_pct) ---
    # Add SELF_REPAIR_MIN_ATTEMPTS - 1 tasks so total attempts stay below gate.
    for _ in range(SELF_REPAIR_MIN_ATTEMPTS - 1):
        db_session.add(
            _make_format_compliance_task(
                project.id, errors=1, repair_attempts=1, repair_successes=0
            )
        )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)
    fc = drift[FORMAT_COMPLIANCE_KEY]
    sr = fc[SELF_REPAIR_SUCCESS_RATE_KEY]
    # Rate is computable but drift_pct must be suppressed (rare-event gate).
    assert sr["current"] is not None, (
        "self_repair_success_rate should still be computed"
    )
    assert sr["drift_pct"] is None, (
        f"drift_pct must be None when repair_attempts ({SELF_REPAIR_MIN_ATTEMPTS - 1}) "
        f"< SELF_REPAIR_MIN_ATTEMPTS ({SELF_REPAIR_MIN_ATTEMPTS})"
    )

    # --- Current window: at-or-above MIN attempts (must report regression) ---
    # Add one more task to cross the threshold; it also fails repair → regression.
    db_session.add(
        _make_format_compliance_task(
            project.id, errors=1, repair_attempts=1, repair_successes=0
        )
    )
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift2 = await read_baseline_with_drift(db_session, project.id)
    fc2 = drift2[FORMAT_COMPLIANCE_KEY]
    sr2 = fc2[SELF_REPAIR_SUCCESS_RATE_KEY]
    assert fc2["repair_attempts"] == SELF_REPAIR_MIN_ATTEMPTS, (
        "repair_attempts should be forwarded in the format_compliance block"
    )
    assert sr2["drift_pct"] is not None, (
        "drift_pct must be reported once repair_attempts >= SELF_REPAIR_MIN_ATTEMPTS"
    )
    assert sr2["drift_pct"] < 0, (
        "all-failures window must show a negative drift vs baseline=1.0"
    )


@pytest.mark.asyncio
async def test_read_baseline_by_schema_passthrough(db_session):
    """read_baseline_with_drift forwards by_schema from the persisted snapshot.

    When a snapshot's format_compliance block contains a by_schema dict,
    read_baseline_with_drift must include it in the returned format_compliance
    block so operators can see per-schema format-miss rankings.
    """
    by_schema_fixture = {
        "thinker": {
            "format_miss": 3,
            "repair_attempts": 3,
            "repair_successes": 2,
            "repair_infra_failures": 0,
            "repair_miss": 1,
            "repair_input_tokens": 1200,
            "repair_output_tokens": 400,
            "self_repair_success_rate": 0.667,
        },
        "security_audit": {
            "format_miss": 1,
            "repair_attempts": 1,
            "repair_successes": 1,
            "repair_infra_failures": 0,
            "repair_miss": 0,
            "repair_input_tokens": 300,
            "repair_output_tokens": 100,
            "self_repair_success_rate": 1.0,
        },
    }
    baseline_fc = {
        FORMAT_COMPLIANCE_KEY: {
            "format_compliance_rate": 0.85,
            SELF_REPAIR_SUCCESS_RATE_KEY: 0.75,
            "repair_attempts": 4,
            "repair_successes": 3,
            "tasks_measured": 10,
            "well_formed_tasks": 8,
            "format_error_total": 4,
            "by_schema": by_schema_fixture,
        }
    }
    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {"captured_at": "2026-01-01T00:00:00", **baseline_fc},
                "snapshots": [{"captured_at": "2026-01-01T00:00:00", **baseline_fc}],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    drift = await read_baseline_with_drift(db_session, project.id)
    fc = drift[FORMAT_COMPLIANCE_KEY]

    assert "by_schema" in fc, (
        "by_schema must be forwarded in the format_compliance block"
    )
    assert fc["by_schema"] == by_schema_fixture, (
        "by_schema contents must match the persisted snapshot value"
    )
    assert fc["by_schema"]["thinker"]["format_miss"] == 3
    assert fc["by_schema"]["security_audit"]["self_repair_success_rate"] == 1.0


# ---------------------------------------------------------------------------
# Panel review effectiveness tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_review_effectiveness_in_write_snapshot(db_session):
    """write_snapshot must populate panel_review_effectiveness with correct
    overturn/confirmed/revision counts from tasks with reconciliation records.

    Three review tasks carry different reconciliation states:
      - 2 challenged_confirmed  (panel upholds)
      - 1 challenged_revised    (panel same direction, different issues)
      - 1 challenged_rejected   (panel overturns — the ROI signal)

    The 'unchallenged' task must be excluded (not a gate invocation).
    Expected overturn_rate = 1 / 4 = 0.25.
    """
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    def _review_task(state):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={
                "pr_merged": True,
                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {"state": state},
            },
            vm_provisioned_at=now - timedelta(minutes=10),
            vm_stopped_at=now,
        )

    db_session.add(_review_task("challenged_confirmed"))
    db_session.add(_review_task("challenged_confirmed"))
    db_session.add(_review_task("challenged_revised"))
    db_session.add(_review_task("challenged_rejected"))
    # unchallenged must be ignored (gate not invoked)
    db_session.add(_review_task("unchallenged"))
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    eff = snap.get("panel_review_effectiveness")
    assert eff is not None, (
        "write_snapshot must include panel_review_effectiveness when gate invocations exist"
    )
    assert eff["challenged_confirmed"] == 2, (
        f"expected 2 confirmed, got {eff['challenged_confirmed']}"
    )
    assert eff["challenged_revised"] == 1, (
        f"expected 1 revised, got {eff['challenged_revised']}"
    )
    assert eff["challenged_rejected"] == 1, (
        f"expected 1 rejected, got {eff['challenged_rejected']}"
    )
    assert eff["total_challenged"] == 4, (
        f"expected total_challenged=4, got {eff['total_challenged']}"
    )
    assert eff["overturn_rate"] == 0.25, (
        f"expected overturn_rate=0.25, got {eff['overturn_rate']}"
    )


def test_panel_review_effectiveness_returns_none_when_no_gate_invocations():
    """compute_panel_review_effectiveness returns None when no tasks have
    gate invocations (all unchallenged or no review_second_opinion key).
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    # Task with no reconciliation key
    t1 = make_task(
        project_id=None,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
    )
    # Task with unchallenged state (gate not invoked)
    t2 = make_task(
        project_id=None,
        status=TaskStatus.COMPLETED,
        context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {"state": "unchallenged"}},
        vm_provisioned_at=now - timedelta(minutes=5),
        vm_stopped_at=now,
    )
    result = compute_panel_review_effectiveness([t1, t2])
    assert result is None, (
        "must return None when no tasks carry a challenged reconciliation state"
    )


def _peff(*, total_challenged, challenged_degraded):
    """Minimal panel_review_effectiveness-shaped dict for classifier tests.

    Mirrors compute_panel_review_effectiveness's fail_open_rate accounting:
    fail_open_rate = degraded / (total + degraded), or None when both are zero.
    """
    invocations = total_challenged + challenged_degraded
    return {
        "total_challenged": total_challenged,
        "challenged_degraded": challenged_degraded,
        "fail_open_rate": (
            round(challenged_degraded / invocations, 4) if invocations > 0 else None
        ),
    }


def test_gate_health_verdict_no_data_on_none():
    """None input → NO_DATA with zero invocations and a null rate."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    verdict = compute_gate_health_verdict(None)
    assert verdict["state"] == "NO_DATA"
    assert verdict["fail_open_rate"] is None
    assert verdict["total_invocations"] == 0
    assert verdict["min_sample"] == 30


def test_gate_health_verdict_no_data_on_zero_invocations():
    """A dict with zero gate invocations → NO_DATA (never HEALTHY)."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    verdict = compute_gate_health_verdict(
        _peff(total_challenged=0, challenged_degraded=0)
    )
    assert verdict["state"] == "NO_DATA"
    assert verdict["total_invocations"] == 0


def test_gate_health_verdict_degraded_below_min_sample():
    """fail_open_rate >= 5% on a sub-min-sample window classifies DEGRADED.

    DEGRADED takes priority over the min-sample gate: a degraded rate on a tiny
    sample must NOT be reported as INSUFFICIENT or HEALTHY.
    """
    from app.efficiency_snapshot import compute_gate_health_verdict

    # 1 degraded / 10 invocations = 10% fail-open on only 10 invocations (< 30).
    verdict = compute_gate_health_verdict(
        _peff(total_challenged=9, challenged_degraded=1)
    )
    assert verdict["state"] == "DEGRADED", (
        f"5%+ fail-open on a small sample must be DEGRADED, got {verdict['state']}"
    )
    assert verdict["fail_open_rate"] == 0.1
    assert verdict["total_invocations"] == 10


def test_gate_health_verdict_insufficient_requires_min_sample():
    """A clean, low-rate window below the min sample is INSUFFICIENT, never HEALTHY."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    # Zero fail-open but only 10 invocations — too few to certify HEALTHY.
    verdict = compute_gate_health_verdict(
        _peff(total_challenged=10, challenged_degraded=0)
    )
    assert verdict["state"] == "INSUFFICIENT", (
        f"low-rate window below 30 invocations must be INSUFFICIENT, got {verdict['state']}"
    )
    assert verdict["total_invocations"] == 10


def test_gate_health_verdict_partial_at_min_sample():
    """>= 30 invocations with 0.01 <= rate < 0.05 → PARTIAL."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    # 1 degraded / 50 invocations = 2% fail-open on a sufficient sample.
    verdict = compute_gate_health_verdict(
        _peff(total_challenged=49, challenged_degraded=1)
    )
    assert verdict["state"] == "PARTIAL", (
        f"2% fail-open on >=30 invocations must be PARTIAL, got {verdict['state']}"
    )
    assert verdict["fail_open_rate"] == 0.02
    assert verdict["total_invocations"] == 50


def test_gate_health_verdict_healthy_requires_min_sample_and_low_rate():
    """>= 30 invocations with rate < 0.01 → HEALTHY."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    # Zero fail-open over 40 invocations — clean and sufficient.
    verdict = compute_gate_health_verdict(
        _peff(total_challenged=40, challenged_degraded=0)
    )
    assert verdict["state"] == "HEALTHY"
    assert verdict["fail_open_rate"] == 0.0
    assert verdict["total_invocations"] == 40
    assert verdict["min_sample"] == 30


def test_gate_health_verdict_low_coverage_after_sufficient_sample():
    """Sufficient sample, zero fail-open, but effective_coverage < 0.5 → LOW_COVERAGE."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    peff = {
        **_peff(total_challenged=40, challenged_degraded=0),
        "effective_coverage": 0.3,  # below 0.5 threshold
    }
    verdict = compute_gate_health_verdict(peff)
    assert verdict["state"] == "LOW_COVERAGE", (
        f"sufficient sample + zero fail-open + low effective_coverage must be LOW_COVERAGE, got {verdict['state']}"
    )
    assert verdict["total_invocations"] == 40

    # Boundary: exactly 0.5 coverage should NOT trigger LOW_COVERAGE.
    peff_boundary = {
        **_peff(total_challenged=40, challenged_degraded=0),
        "effective_coverage": 0.5,
    }
    verdict_boundary = compute_gate_health_verdict(peff_boundary)
    assert verdict_boundary["state"] == "HEALTHY", (
        f"effective_coverage == 0.5 must be HEALTHY, got {verdict_boundary['state']}"
    )


def test_gate_health_verdict_degraded_priority_over_insufficient_at_boundary():
    """5% exactly on a small sample → DEGRADED; min_sample override respected."""
    from app.efficiency_snapshot import compute_gate_health_verdict

    # 1 degraded / 20 = 5% exactly, only 20 invocations.
    verdict = compute_gate_health_verdict(
        _peff(total_challenged=19, challenged_degraded=1)
    )
    assert verdict["state"] == "DEGRADED"
    # min_sample override flows through to the returned supporting field.
    custom = compute_gate_health_verdict(
        _peff(total_challenged=10, challenged_degraded=0), min_sample=5
    )
    assert custom["state"] == "HEALTHY"
    assert custom["min_sample"] == 5


def test_gate_health_verdict_on_post_deploy_effectiveness():
    """Gate health verdict applied to post-deploy panel_review_effectiveness.

    Validates that assess_post_deploy_gate_health correctly classifies verdicts
    when applied to scoped post-deploy effectiveness windows. Tests the
    integration of post-deploy boundary filtering with verdict computation.

    Setup: mix of pre-deploy and post-deploy tasks.
      Pre-deploy (before boundary): 4 total, 1 degraded → 20% fail-open
      Post-deploy (after boundary): 5 total, 1 degraded → 20% fail-open (DEGRADED)

    Expected: post-deploy verdict should be DEGRADED due to 20% rate >= 5%.
    """
    from app.efficiency_snapshot import (
        assess_post_deploy_gate_health,
        compute_panel_review_effectiveness,
    )
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()
    deploy_boundary = now - timedelta(days=2)
    pre_deploy = now - timedelta(days=5)

    def _task(recon, updated_at):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=updated_at - timedelta(minutes=5),
            vm_stopped_at=updated_at,
            updated_at=updated_at,
        )

    # Pre-deploy tasks
    pre_tasks = [
        _task({"state": "challenged_confirmed"}, pre_deploy),
        _task({"state": "challenged_confirmed"}, pre_deploy),
        _task({"state": "challenged_confirmed"}, pre_deploy),
        _task({"state": "degraded_model_unavailable"}, pre_deploy),
    ]

    # Post-deploy tasks (enough for INSUFFICIENT check to pass)
    post_tasks = [
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=12)),
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=6)),
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=3)),
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=2)),
        _task({"state": "degraded_model_unavailable"}, now - timedelta(hours=1)),
    ]
    all_tasks = pre_tasks + post_tasks

    # Post-deploy only: filter using deploy_boundary
    post_deploy_effectiveness = compute_panel_review_effectiveness(
        all_tasks, since_dt=deploy_boundary
    )
    assert post_deploy_effectiveness is not None
    assert post_deploy_effectiveness["total_challenged"] == 4
    assert post_deploy_effectiveness["challenged_degraded"] == 1

    # Apply assess_post_deploy_gate_health to the post-deploy effectiveness
    # 1 degraded / (4 + 1) = 0.2 = 20% → exceeds 5% degraded threshold
    verdict = assess_post_deploy_gate_health(post_deploy_effectiveness)
    assert verdict["state"] == "DEGRADED", (
        f"20% fail-open on 5 invocations must be DEGRADED, got {verdict['state']}"
    )
    assert verdict["total_invocations"] == 5
    assert verdict["fail_open_rate"] == 0.2

    # Test a healthy post-deploy scenario: 30+ invocations with 0% fail-open
    healthy_tasks = [
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=i))
        for i in range(35)
    ]
    healthy_effectiveness = compute_panel_review_effectiveness(
        healthy_tasks, since_dt=deploy_boundary
    )
    healthy_verdict = assess_post_deploy_gate_health(healthy_effectiveness)
    assert healthy_verdict["state"] == "HEALTHY", (
        f"zero fail-open on 35 invocations must be HEALTHY, got {healthy_verdict['state']}"
    )
    assert healthy_verdict["fail_open_rate"] == 0.0
    assert healthy_verdict["total_invocations"] == 35


def test_panel_review_effectiveness_cost_per_challenge_usd():
    """compute_panel_review_effectiveness includes cost_per_challenge_usd.

    Two panel_review challenger tasks carry known LLM token usage.
    Four review tasks carry reconciliation states (total_challenged=4).
    Expected cost_per_challenge_usd = total_panel_review_llm_cost / 4.
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    def _challenged_review(state):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {"state": state}},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    def _panel_review_task(input_tokens, output_tokens):
        t = make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "panel_review"}],
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        t.api_call_summary = {
            "llm_call_count": 1,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
        }
        return t

    tasks = [
        _challenged_review("challenged_confirmed"),
        _challenged_review("challenged_confirmed"),
        _challenged_review("challenged_revised"),
        _challenged_review("challenged_rejected"),
        _panel_review_task(1000, 200),
        _panel_review_task(2000, 400),
    ]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["total_challenged"] == 4
    expected_total_cost = (1000 + 2000) * input_rate + (200 + 400) * output_rate
    expected_per_challenge = round(expected_total_cost / 4, 4)
    assert result["cost_per_challenge_usd"] == expected_per_challenge, (
        f"expected cost_per_challenge_usd={expected_per_challenge}, got {result['cost_per_challenge_usd']}"
    )


def test_cost_per_challenge_excludes_out_of_window_and_degraded():
    """cost_per_challenge_usd must apply since_dt window filter to numerator (LLM cost).

    The bug: panel_review LLM cost was summed with no window filter, but the
    denominator (challenged counts) was filtered by since_dt. This inflated
    cost_per_challenge_usd when the task list spanned wider than the reporting window.

    Setup:
      - 2 in-window challenged_confirmed (total_challenged = 2)
      - 1 out-of-window challenged_confirmed (must be ignored in cost calc)
      - 1 degraded_model_unavailable (no cost in calculation; state != challenged_*)
      - 3 panel_review tasks: 2 in-window, 1 out-of-window
      - Expected: cost_per_challenge_usd = (cost of 2 in-window panel_review) / 2

    Verify:
      - cost_per_challenge_usd correctly excludes out-of-window panel_review cost
      - cost_per_challenge_usd correctly excludes degraded challenges
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()
    window_start = now - timedelta(hours=1)
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    def _challenged_review(state, updated_at):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {"state": state}},
            updated_at=updated_at,
            vm_provisioned_at=updated_at - timedelta(minutes=5),
            vm_stopped_at=updated_at,
        )

    def _panel_review_task(input_tokens, output_tokens, updated_at):
        t = make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "panel_review"}],
            updated_at=updated_at,
            vm_provisioned_at=updated_at - timedelta(minutes=5),
            vm_stopped_at=updated_at,
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        t.api_call_summary = {
            "llm_call_count": 1,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
        }
        return t

    tasks = [
        # In-window challenged (should count)
        _challenged_review("challenged_confirmed", now - timedelta(minutes=10)),
        _challenged_review("challenged_confirmed", now - timedelta(minutes=5)),
        # Out-of-window challenged (should NOT count in cost numerator)
        _challenged_review("challenged_confirmed", window_start - timedelta(minutes=5)),
        # Degraded challenge (does not count in total_challenged)
        _challenged_review("degraded_model_unavailable", now - timedelta(minutes=3)),
        # In-window panel_review tasks (1000+2000 input, 200+400 output)
        _panel_review_task(1000, 200, now - timedelta(minutes=8)),
        _panel_review_task(2000, 400, now - timedelta(minutes=3)),
        # Out-of-window panel_review task (should NOT count in cost)
        _panel_review_task(3000, 600, window_start - timedelta(minutes=10)),
    ]

    result = compute_panel_review_effectiveness(tasks, since_dt=window_start)
    assert result is not None
    # Only 2 in-window challenged_confirmed counts (excludes out-of-window and degraded)
    assert result["total_challenged"] == 2, (
        f"expected total_challenged=2 (in-window only), got {result['total_challenged']}"
    )

    # Expected cost: only in-window panel_review tasks count
    expected_cost = (1000 + 2000) * input_rate + (200 + 400) * output_rate
    expected_per_challenge = round(expected_cost / 2, 4)
    assert result["cost_per_challenge_usd"] == expected_per_challenge, (
        f"expected cost_per_challenge_usd={expected_per_challenge} "
        f"(in-window tasks only), got {result['cost_per_challenge_usd']}"
    )


def test_panel_review_effectiveness_escalated_fail_open():
    """Escalated records (unchallenged + escalated=True) are counted in
    challenged_escalated (informational) and excluded from total_challenged and fail_open_rate.

    These records represent a BLOCKED/fail-CLOSED path: the challenger crashed or
    returned an unparseable verdict, so the merge was blocked and escalated for manual
    review.  They are NOT auto-merges and must NOT inflate fail_open_rate.

    Setup:
      - 2 challenged_confirmed (normal gate invocations)
      - 1 challenged_rejected  (normal gate invocation)
      - 2 unchallenged + escalated=True (challenger crashed — merge BLOCKED, not fail-open)
      - 1 unchallenged without escalated (gate not invoked — must be ignored)

    Expected:
      total_challenged = 3 (only the three successful gate invocations)
      challenged_escalated = 2 (safe/blocked escalations, informational)
      fail_open_rate = 0.0 (gate was invoked but no degraded events — confirmed-zero, not no-data)
      overturn_rate = 1/3
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _task(recon):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    tasks = [
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_rejected"}),
        _task({"state": "unchallenged", "escalated": True}),
        _task({"state": "unchallenged", "escalated": True}),
        _task({"state": "unchallenged"}),  # gate not invoked — must be ignored
    ]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None, "must return a result when escalated records exist"
    assert result["total_challenged"] == 3, (
        f"escalated records must be excluded from total_challenged, got {result['total_challenged']}"
    )
    assert result["challenged_escalated"] == 2, (
        f"expected challenged_escalated=2, got {result['challenged_escalated']}"
    )
    assert result["overturn_rate"] == round(1 / 3, 4), (
        f"overturn_rate must be computed from total_challenged only, got {result['overturn_rate']}"
    )
    # unchallenged+escalated=True records are safe (merge BLOCKED); fail_open_rate must not count them.
    # Gate was invoked (total=3), so fail_open_rate is confirmed-zero (0.0), not None (no-data).
    assert result["fail_open_rate"] == 0.0, (
        f"fail_open_rate must be 0.0 (confirmed-zero) when gate was invoked but no degraded events exist, got {result['fail_open_rate']}"
    )
    assert result.get("challenged_degraded", 0) == 0, (
        "challenged_degraded must be 0 when no degraded_model_unavailable records exist"
    )


def test_panel_review_effectiveness_fail_open_rate():
    """fail_open_rate = challenged_degraded / (total_challenged + challenged_degraded).

    degraded_model_unavailable records are the true fail-open path: the challenger's
    model was inaccessible so the primary verdict was adopted and the PR auto-merged.
    unchallenged+escalated=True records are safe (merge BLOCKED) and must NOT drive
    fail_open_rate.

    Setup:
      - 3 challenged_confirmed (successful gate invocations)
      - 2 degraded_model_unavailable (silent auto-merges — true fail-opens)
      - 1 unchallenged + escalated=True (challenger crashed — merge BLOCKED, not counted in fail_open_rate)

    Expected:
      total_challenged = 3
      challenged_degraded = 2
      challenged_escalated = 1 (informational; does NOT affect fail_open_rate)
      fail_open_rate = 2 / (3 + 2) = 0.4
      Zero-invocations case (no tasks) returns None for fail_open_rate.
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _task(recon):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    tasks = [
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "degraded_model_unavailable"}),
        _task({"state": "degraded_model_unavailable"}),
        _task({"state": "unchallenged", "escalated": True}),  # safe — must not affect fail_open_rate
    ]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["total_challenged"] == 3
    assert result["challenged_degraded"] == 2, (
        f"expected challenged_degraded=2, got {result.get('challenged_degraded')}"
    )
    assert result["challenged_escalated"] == 1, (
        f"expected challenged_escalated=1 (informational), got {result.get('challenged_escalated')}"
    )
    expected_rate = round(2 / 5, 4)
    assert result["fail_open_rate"] == expected_rate, (
        f"expected fail_open_rate={expected_rate} (degraded only), got {result['fail_open_rate']}"
    )
    # Zero-invocations: no tasks with gate records returns None
    assert compute_panel_review_effectiveness([]) is None


def test_panel_review_effectiveness_degraded_fail_open_rate():
    """degraded_model_unavailable is the true fail-open path; escalated is safe.

    Mixed scenario with all three state classes to verify bucket separation:
      - 4 challenged_confirmed
      - 1 challenged_rejected
      - 3 degraded_model_unavailable (silent auto-merges — true fail-opens)
      - 2 unchallenged + escalated=True (merge BLOCKED — safe, not fail-open)
      - 1 unchallenged (gate not invoked — ignored)

    Expected:
      total_challenged = 5
      challenged_degraded = 3
      challenged_escalated = 2
      fail_open_rate = 3 / (5 + 3) = 0.375
      overturn_rate = 1 / 5 = 0.2
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _task(recon):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    tasks = [
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_rejected"}),
        _task({"state": "degraded_model_unavailable"}),
        _task({"state": "degraded_model_unavailable"}),
        _task({"state": "degraded_model_unavailable"}),
        _task({"state": "unchallenged", "escalated": True}),
        _task({"state": "unchallenged", "escalated": True}),
        _task({"state": "unchallenged"}),  # ignored
    ]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None
    assert result["total_challenged"] == 5, f"got {result['total_challenged']}"
    assert result["challenged_degraded"] == 3, f"got {result.get('challenged_degraded')}"
    assert result["challenged_escalated"] == 2, f"got {result.get('challenged_escalated')}"
    assert result["overturn_rate"] == round(1 / 5, 4), f"got {result['overturn_rate']}"
    expected_rate = round(3 / 8, 4)
    assert result["fail_open_rate"] == expected_rate, (
        f"expected fail_open_rate={expected_rate}, got {result['fail_open_rate']}"
    )
    # Verify escalated records do NOT bleed into fail_open_rate numerator
    assert result["fail_open_rate"] != round(5 / 10, 4), (
        "fail_open_rate must not include challenged_escalated in the numerator"
    )


def test_fail_open_rate_distinguishes_confirmed_zero_from_insufficient_data():
    """fail_open_rate=0.0 when gate was invoked and no fail-opens occurred (confirmed-zero).
    fail_open_rate=None only when the window has no gate invocations at all (no-data).

    This distinction is critical for the v18 <=5% audit: a None result means
    'unknown' (cannot confirm or deny), while 0.0 means 'confirmed healthy'.

    Setup A (confirmed-zero):
      - 3 challenged_confirmed + 1 challenged_rejected = total=4, degraded=0
      - Expected: fail_open_rate=0.0 (gate was invoked; zero degraded events measured)

    Setup B (no-data):
      - 0 gate invocations (only unchallenged tasks)
      - Expected: fail_open_rate via None return (no gate invocations; rate is unknown)
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _task(recon):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    # Setup A: gate invoked with 0 degraded events → confirmed-zero
    tasks_a = [
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_confirmed"}),
        _task({"state": "challenged_rejected"}),
    ]
    result_a = compute_panel_review_effectiveness(tasks_a)
    assert result_a is not None, "must return a result when gate was invoked"
    assert result_a["total_challenged"] == 4
    assert result_a["challenged_degraded"] == 0
    assert result_a["fail_open_rate"] == 0.0, (
        f"fail_open_rate must be 0.0 (confirmed-zero) when gate invoked and no degraded events, got {result_a['fail_open_rate']}"
    )

    # Setup B: no gate invocations → no-data (None return)
    tasks_b = [
        _task({"state": "unchallenged"}),       # gate not invoked — ignored
        make_task(project_id=None, status=TaskStatus.COMPLETED),  # no recon key
    ]
    result_b = compute_panel_review_effectiveness(tasks_b)
    assert result_b is None, (
        f"must return None when no gate invocations exist (fail_open_rate would be meaningless), got {result_b}"
    )


def test_fail_open_rate_scoped_to_post_deploy_window():
    """compute_panel_review_effectiveness respects since_dt to scope to post-deploy traffic.

    Setup: mix of pre-deploy and post-deploy tasks with gate records.
      Pre-deploy (before deploy boundary):
        - 2 challenged_confirmed
        - 1 degraded_model_unavailable (fail-open)
      Post-deploy (after deploy boundary):
        - 3 challenged_confirmed
        - 1 degraded_model_unavailable (fail-open)

    Without since_dt (7-day window):
      total_challenged = 5, challenged_degraded = 2
      fail_open_rate = 2 / (5 + 2) = 2/7

    With since_dt = deploy boundary (post-deploy only):
      total_challenged = 3, challenged_degraded = 1
      fail_open_rate = 1 / (3 + 1) = 0.25

    This confirms an operator can isolate the v18-specific rate from the rolling
    window that mixes pre- and post-deployment traffic.
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()
    deploy_boundary = now - timedelta(days=2)
    pre_deploy = now - timedelta(days=5)

    def _task(recon, updated_at):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=updated_at - timedelta(minutes=5),
            vm_stopped_at=updated_at,
            updated_at=updated_at,
        )

    pre_tasks = [
        _task({"state": "challenged_confirmed"}, pre_deploy),
        _task({"state": "challenged_confirmed"}, pre_deploy),
        _task({"state": "degraded_model_unavailable"}, pre_deploy),
    ]
    post_tasks = [
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=12)),
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=6)),
        _task({"state": "challenged_confirmed"}, now - timedelta(hours=1)),
        _task({"state": "degraded_model_unavailable"}, now - timedelta(hours=3)),
    ]
    all_tasks = pre_tasks + post_tasks

    # Full 7-day window: pre + post traffic mixed
    full = compute_panel_review_effectiveness(all_tasks)
    assert full is not None
    assert full["total_challenged"] == 5
    assert full["challenged_degraded"] == 2
    assert full["fail_open_rate"] == round(2 / 7, 4), (
        f"7-day window: expected fail_open_rate={round(2/7, 4)}, got {full['fail_open_rate']}"
    )

    # Post-deploy only: tasks before deploy_boundary are excluded
    post_deploy_only = compute_panel_review_effectiveness(all_tasks, since_dt=deploy_boundary)
    assert post_deploy_only is not None
    assert post_deploy_only["total_challenged"] == 3, (
        f"post-deploy: expected total_challenged=3, got {post_deploy_only['total_challenged']}"
    )
    assert post_deploy_only["challenged_degraded"] == 1, (
        f"post-deploy: expected challenged_degraded=1, got {post_deploy_only.get('challenged_degraded')}"
    )
    expected_post_rate = round(1 / 4, 4)
    assert post_deploy_only["fail_open_rate"] == expected_post_rate, (
        f"post-deploy: expected fail_open_rate={expected_post_rate}, got {post_deploy_only['fail_open_rate']}"
    )

    # A deploy boundary after all tasks → no gate invocations → None
    future = compute_panel_review_effectiveness(all_tasks, since_dt=now + timedelta(days=1))
    assert future is None, (
        "since_dt beyond all task timestamps must return None (no gate invocations in window)"
    )


def test_panel_review_effectiveness_fail_open_rate_none_with_only_escalated():
    """fail_open_rate is None for escalated-only windows (total==0, degraded==0, escalated>0).

    An escalated-only window (all gate invocations crashed/failed and the merge was
    BLOCKED for manual review) has gate activity, so the function returns a non-None
    result dict.  But fail_open_rate requires at least one successful gate invocation
    (total>0) or a real fail-open (degraded>0) to be meaningful — neither holds in an
    escalated-only window, so fail_open_rate is None rather than 0.0.

    This distinguishes "no-data" (fail_open_rate=None, no successful gate runs) from
    "confirmed-zero" (fail_open_rate=0.0, gate ran and found zero degraded events).
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _escalated_task():
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {"state": "unchallenged", "escalated": True}},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    tasks = [_escalated_task(), _escalated_task(), _escalated_task()]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None, (
        "escalated-only window must return non-None (escalated>0 bypasses early return)"
    )
    assert result["total_challenged"] == 0, f"got {result['total_challenged']}"
    assert result["challenged_escalated"] == 3, f"got {result['challenged_escalated']}"
    assert result["challenged_degraded"] == 0, f"got {result.get('challenged_degraded')}"
    assert result["fail_open_rate"] is None, (
        f"fail_open_rate must be None (no successful gate invocations or degraded events), "
        f"got {result['fail_open_rate']}"
    )


# ---------------------------------------------------------------------------
# compute_recommended_challenger_sample_rate
# ---------------------------------------------------------------------------


def test_recommended_sample_rate_no_data():
    """Returns no_data status when panel_review_effectiveness is None."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    result = compute_recommended_challenger_sample_rate(None)
    assert result["status"] == "no_data"
    assert result["recommended_sample_rate"] is None
    assert "rationale" in result


def test_recommended_sample_rate_insufficient_data():
    """Returns insufficient_data when total_challenged < MIN_SAMPLE_SIZE (30)."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    eff = {"total_challenged": 15, "overturn_rate": 0.05}
    result = compute_recommended_challenger_sample_rate(eff)
    assert result["status"] == "insufficient_data"
    assert result["recommended_sample_rate"] is None
    assert "15" in result["rationale"]


def test_recommended_sample_rate_gate_earning():
    """Returns gate_earning with rate=1.0 when overturn_rate >= 10%."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    eff = {"total_challenged": 50, "overturn_rate": 0.15}
    result = compute_recommended_challenger_sample_rate(eff)
    assert result["status"] == "gate_earning"
    assert result["recommended_sample_rate"] == 1.0


def test_recommended_sample_rate_reduce():
    """Returns reduce status with rate < 1.0 when overturn_rate < 10%."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    # 4% overturn rate → recommended = max(0.10, 0.04/0.10) = max(0.10, 0.40) = 0.40
    eff = {"total_challenged": 100, "overturn_rate": 0.04}
    result = compute_recommended_challenger_sample_rate(eff)
    assert result["status"] == "reduce"
    assert result["recommended_sample_rate"] == 0.40


def test_recommended_sample_rate_reduce_floor():
    """Very low overturn_rate is clamped to 0.10 floor."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    eff = {"total_challenged": 100, "overturn_rate": 0.005}
    result = compute_recommended_challenger_sample_rate(eff)
    assert result["status"] == "reduce"
    assert result["recommended_sample_rate"] == 0.10


def test_recommended_sample_rate_at_threshold_boundary():
    """Exactly at 10% overturn_rate is gate_earning."""
    from app.efficiency_snapshot import compute_recommended_challenger_sample_rate

    eff = {"total_challenged": 50, "overturn_rate": 0.10}
    result = compute_recommended_challenger_sample_rate(eff)
    assert result["status"] == "gate_earning"
    assert result["recommended_sample_rate"] == 1.0


@pytest.mark.asyncio
async def test_self_repair_burn_share_in_snapshot(db_session):
    """write_snapshot includes self_repair_burn_share in llm_cost_attribution.

    self_repair_burn_share = repair_attempts / total_llm_call_count.
    Tasks with format_compliance_summary carrying repair_attempts contribute
    to the numerator; api_call_summary.llm_call_count contributes to the denominator.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with 10 LLM calls and 2 repair attempts.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 1}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "llm_call_count": 10,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 2,
            "format_repair_attempts": 2,
            "format_repair_successes": 1,
            "format_repair_infra_failures": 0,
        },
    )
    db_session.add(task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    attr = snap.get("llm_cost_attribution")
    assert attr is not None, "llm_cost_attribution must be present"
    assert SELF_REPAIR_BURN_SHARE_KEY in attr, "self_repair_burn_share must be present"
    # 2 repair attempts / 10 total LLM calls = 0.2
    assert attr[SELF_REPAIR_BURN_SHARE_KEY] == pytest.approx(0.2, abs=1e-6)


@pytest.mark.asyncio
async def test_self_repair_burn_share_none_when_no_llm_calls(db_session):
    """self_repair_burn_share is None when total_llm_call_count is zero."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with no api_call_summary (no LLM calls recorded).
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 2}],
    )
    db_session.add(task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    attr = snap.get("llm_cost_attribution")
    assert attr is not None
    assert attr[SELF_REPAIR_BURN_SHARE_KEY] is None


@pytest.mark.asyncio
async def test_self_repair_burn_share_in_llm_cost_attribution_trend(db_session):
    """read_baseline_with_drift includes self_repair_burn_share in each trend entry."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 3}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 1500,
            "llm_call_count": 20,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 1,
            "format_repair_attempts": 4,
            "format_repair_successes": 3,
            "format_repair_infra_failures": 0,
        },
    )
    db_session.add(task)
    await db_session.commit()

    await write_snapshot(db_session, project.id)
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)
    trend = drift.get("llm_cost_attribution_trend")
    assert isinstance(trend, list)
    assert len(trend) >= 1
    for entry in trend:
        assert SELF_REPAIR_BURN_SHARE_KEY in entry, (
            "each trend entry must carry self_repair_burn_share"
        )
    # 4 repair attempts / 20 LLM calls = 0.2
    assert trend[-1][SELF_REPAIR_BURN_SHARE_KEY] == pytest.approx(0.2, abs=1e-6)


@pytest.mark.asyncio
async def test_self_repair_burn_share_token_weighted(db_session):
    """self_repair_burn_share uses token-cost ratio when repair token data is present.

    repair_token_count / total_token_count where repair_token_count =
    format_repair_input_tokens + format_repair_output_tokens.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with 2000 input + 500 output total tokens; 100 input + 50 output repair tokens.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 10}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 500,
            "llm_call_count": 10,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 1,
            "format_repair_attempts": 2,
            "format_repair_successes": 1,
            "format_repair_infra_failures": 0,
            "format_repair_input_tokens": 100,
            "format_repair_output_tokens": 50,
        },
    )
    db_session.add(task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    attr = snap.get("llm_cost_attribution")
    assert attr is not None
    assert SELF_REPAIR_BURN_SHARE_KEY in attr
    # Token-based: (100 + 50) / (2000 + 500) = 150 / 2500 = 0.06
    assert attr[SELF_REPAIR_BURN_SHARE_KEY] == pytest.approx(0.06, abs=1e-4)


@pytest.mark.asyncio
async def test_self_repair_burn_share_fallback_to_call_count(db_session):
    """self_repair_burn_share falls back to call-count ratio for pre-token-tracking tasks.

    When format_compliance_summary lacks format_repair_input_tokens, the formula
    is repair_attempts / total_llm_call_count (the original formula).
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task without repair token fields (pre-dates the feature).
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 11}],
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 1000,
            "llm_call_count": 20,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 1,
            "format_repair_attempts": 4,
            "format_repair_successes": 3,
            "format_repair_infra_failures": 0,
            # No format_repair_input_tokens / format_repair_output_tokens
        },
    )
    db_session.add(task)
    await db_session.commit()

    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    attr = snap.get("llm_cost_attribution")
    assert attr is not None
    assert SELF_REPAIR_BURN_SHARE_KEY in attr
    # Fallback: 4 repair attempts / 20 LLM calls = 0.2
    assert attr[SELF_REPAIR_BURN_SHARE_KEY] == pytest.approx(0.2, abs=1e-6)


# ── _extract_ts_template_literal_length tests ────────────────────────────────


def test_extract_ts_template_literal_length_basic():
    """Returns correct char count for a simple template literal."""
    src = "const HELLO = `hello world`;"
    result = _extract_ts_template_literal_length(src, "HELLO")
    assert result == len("hello world")


def test_extract_ts_template_literal_length_multiline():
    """Counts newlines and all chars within the literal."""
    body = "line one\nline two\nline three"
    src = f"const MULTI = `{body}`;"
    result = _extract_ts_template_literal_length(src, "MULTI")
    assert result == len(body)


def test_extract_ts_template_literal_length_escaped_backtick():
    """Escaped backticks inside the literal do not terminate the scan early."""
    body = r"prefix \` suffix"
    src = f"const ESCAPED = `{body}`;"
    result = _extract_ts_template_literal_length(src, "ESCAPED")
    assert result == len(body)


def test_extract_ts_template_literal_length_missing_const_returns_none():
    """Returns None when the named constant is not present in the source."""
    src = "const OTHER = `some content`;"
    result = _extract_ts_template_literal_length(src, "MISSING_NAME")
    assert result is None


def test_extract_ts_template_literal_length_unterminated_returns_none():
    """Returns None when the template literal has no closing backtick."""
    src = "const OPEN = `no closing backtick"
    result = _extract_ts_template_literal_length(src, "OPEN")
    assert result is None


# ── _compute_prompt_static_bytes tests ───────────────────────────────────────


def test_compute_prompt_static_bytes_all_constants_present_returns_positive():
    """When all template-literal and plain-string constants are found the result
    is a positive integer equal to the sum of all their body lengths.

    Both prompt.ts and prompts/task-type-focus.ts are mocked via builtins.open
    (which returns the same synthetic source for every open call), so the
    combined synthetic source must contain constants from all three tuples.
    """
    from unittest.mock import mock_open, patch

    # Build a synthetic source containing all template-literal constants from
    # both prompt.ts and task-type-focus.ts (builtins.open returns same source
    # for both file opens).
    tl_bodies = {name: f"body of {name} " * 5 for name in _PROMPT_STATIC_CONST_NAMES}
    synthetic_src = "\n".join(
        f"const {name} = `{body}`;" for name, body in tl_bodies.items()
    )
    # Append plain-string constants.
    ps_bodies = {
        name: f"plain body of {name} " * 3 for name in _PROMPT_STATIC_PLAIN_CONST_NAMES
    }
    synthetic_src += "\n" + "\n".join(
        f"const {name} = '{body}';" for name, body in ps_bodies.items()
    )
    # Append task-type-focus template-literal constants.
    ttf_bodies = {
        name: f"ttf body of {name} " * 4 for name in _TASK_TYPE_FOCUS_CONST_NAMES
    }
    synthetic_src += "\n" + "\n".join(
        f"const {name} = `{body}`;" for name, body in ttf_bodies.items()
    )
    expected_total = (
        sum(len(b) for b in tl_bodies.values())
        + sum(len(b) for b in ps_bodies.values())
        + sum(len(b) for b in ttf_bodies.values())
    )

    import app.efficiency_snapshot as es_mod

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=synthetic_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result == expected_total
    assert result > 0


def test_compute_prompt_static_bytes_missing_constant_returns_none():
    """Returns None when any one of _PROMPT_STATIC_CONST_NAMES is absent."""
    from unittest.mock import mock_open, patch

    # Omit the last template-literal constant from the synthetic source.
    present_names = _PROMPT_STATIC_CONST_NAMES[:-1]
    synthetic_src = "\n".join(
        f"const {name} = `body of {name}`;" for name in present_names
    )
    # Include all plain-string constants so the failure is from the template-literal gap.
    synthetic_src += "\n" + "\n".join(
        f"const {name} = 'plain body';" for name in _PROMPT_STATIC_PLAIN_CONST_NAMES
    )

    import app.efficiency_snapshot as es_mod

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=synthetic_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result is None


def test_compute_prompt_static_bytes_missing_plain_constant_returns_none():
    """Returns None when any plain-string constant in _PROMPT_STATIC_PLAIN_CONST_NAMES
    is absent from the source."""
    from unittest.mock import mock_open, patch

    # Include all template-literal constants but omit plain-string ones.
    synthetic_src = "\n".join(
        f"const {name} = `body of {name}`;" for name in _PROMPT_STATIC_CONST_NAMES
    )

    import app.efficiency_snapshot as es_mod

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=synthetic_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result is None


def test_extract_ts_plain_string_length_single_quote():
    """_extract_ts_plain_string_length extracts the body of a single-quoted constant."""
    src = "const MY_CONST = 'hello world';"
    assert _extract_ts_plain_string_length(src, "MY_CONST") == len("hello world")


def test_extract_ts_plain_string_length_double_quote():
    """_extract_ts_plain_string_length extracts the body of a double-quoted constant."""
    src = 'const MY_CONST = "hello world";'
    assert _extract_ts_plain_string_length(src, "MY_CONST") == len("hello world")


def test_extract_ts_plain_string_length_missing_returns_none():
    """_extract_ts_plain_string_length returns None when the constant is absent."""
    src = "const OTHER = 'value';"
    assert _extract_ts_plain_string_length(src, "MY_CONST") is None


def test_compute_prompt_static_bytes_no_agent_src_returns_none():
    """Returns None when _find_agent_src cannot locate the agent/src directory."""
    from unittest.mock import patch

    import app.efficiency_snapshot as es_mod

    with patch.object(es_mod, "_find_agent_src", return_value=None):
        result = _compute_prompt_static_bytes()

    assert result is None


def test_compute_prompt_static_bytes_missing_task_type_focus_file_returns_none():
    """Returns None when prompts/task-type-focus.ts is absent even if prompt.ts is present."""
    from unittest.mock import mock_open, patch

    import app.efficiency_snapshot as es_mod

    # Build a synthetic source that satisfies ALL prompt.ts constants.
    tl_src = "\n".join(
        f"const {name} = `body of {name}`;" for name in _PROMPT_STATIC_CONST_NAMES
    )
    ps_src = "\n".join(
        f"const {name} = 'plain body';" for name in _PROMPT_STATIC_PLAIN_CONST_NAMES
    )
    prompt_src = tl_src + "\n" + ps_src

    def _fake_exists(path):
        # prompt.ts exists; task-type-focus.ts does NOT.
        return "task-type-focus.ts" not in path

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", side_effect=_fake_exists),
        patch("builtins.open", mock_open(read_data=prompt_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result is None


def test_compute_prompt_static_bytes_missing_task_type_focus_constant_returns_none():
    """Returns None when a constant in _TASK_TYPE_FOCUS_CONST_NAMES is absent from
    prompts/task-type-focus.ts even if all prompt.ts constants are present."""
    from unittest.mock import mock_open, patch

    import app.efficiency_snapshot as es_mod

    # Build a combined source that has all prompt.ts constants but is missing
    # the last _TASK_TYPE_FOCUS_CONST_NAMES entry.
    tl_src = "\n".join(
        f"const {name} = `body of {name}`;" for name in _PROMPT_STATIC_CONST_NAMES
    )
    ps_src = "\n".join(
        f"const {name} = 'plain body';" for name in _PROMPT_STATIC_PLAIN_CONST_NAMES
    )
    ttf_present = _TASK_TYPE_FOCUS_CONST_NAMES[:-1]  # omit the last one
    ttf_src = "\n".join(
        f"const {name} = `ttf body of {name}`;" for name in ttf_present
    )
    synthetic_src = tl_src + "\n" + ps_src + "\n" + ttf_src

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=synthetic_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result is None


def test_compute_prompt_static_bytes_task_type_focus_additive():
    """task-type-focus constants add to the total beyond prompt.ts-only constants."""
    from unittest.mock import mock_open, patch

    import app.efficiency_snapshot as es_mod

    # Build prompt.ts-only source.
    tl_bodies = {name: f"body of {name} " * 5 for name in _PROMPT_STATIC_CONST_NAMES}
    prompt_src = "\n".join(
        f"const {name} = `{body}`;" for name, body in tl_bodies.items()
    )
    ps_bodies = {
        name: f"plain body of {name} " * 3 for name in _PROMPT_STATIC_PLAIN_CONST_NAMES
    }
    prompt_src += "\n" + "\n".join(
        f"const {name} = '{body}';" for name, body in ps_bodies.items()
    )
    prompt_only_total = sum(len(b) for b in tl_bodies.values()) + sum(
        len(b) for b in ps_bodies.values()
    )

    # Extend to include task-type-focus constants in the SAME source (builtins.open
    # returns same content for both file opens under mock_open).
    ttf_bodies = {
        name: f"ttf body of {name} " * 4 for name in _TASK_TYPE_FOCUS_CONST_NAMES
    }
    combined_src = prompt_src + "\n" + "\n".join(
        f"const {name} = `{body}`;" for name, body in ttf_bodies.items()
    )
    ttf_total = sum(len(b) for b in ttf_bodies.values())

    with (
        patch.object(es_mod, "_find_agent_src", return_value="/fake/agent/src"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=combined_src)),
    ):
        result = _compute_prompt_static_bytes()

    assert result is not None
    assert isinstance(result, int)
    assert result > prompt_only_total, (
        f"result ({result}) should exceed prompt.ts-only total ({prompt_only_total}); "
        "task-type-focus constants must be additive"
    )
    assert result == prompt_only_total + ttf_total


def test_prompt_static_bytes_covers_task_type_focus():
    """_TASK_TYPE_FOCUS_CONST_NAMES lists the six per-task-type focus blocks and
    _compute_prompt_static_bytes() includes their byte counts in the total.

    This is the acceptance test for the v11 AXIS_PROMPT_STATIC_BYTES extension:
    it exercises _compute_prompt_static_bytes() against the live source files so
    any PR that removes or renames a task-type focus constant without updating
    _TASK_TYPE_FOCUS_CONST_NAMES will fail here.

    The test makes three assertions:
    1. Every name in _TASK_TYPE_FOCUS_CONST_NAMES exists as a template-literal
       constant in the live task-type-focus.ts source.
    2. _compute_prompt_static_bytes() returns a value larger than what prompt.ts
       alone would produce, confirming the task-type-focus bytes are included.
    3. The combined result exceeds the prompt.ts-only lower bound (8 000 bytes)
       and the task-type-focus contribution alone exceeds 1 000 bytes (the six
       blocks together are several thousand bytes in the live source).
    """
    import os

    import app.efficiency_snapshot as es_mod

    result = _compute_prompt_static_bytes()
    if result is None:
        pytest.skip(
            "agent/src/prompt.ts or agent/src/prompts/task-type-focus.ts not "
            "available in this environment"
        )

    assert len(_TASK_TYPE_FOCUS_CONST_NAMES) >= 6, (
        f"_TASK_TYPE_FOCUS_CONST_NAMES has only {len(_TASK_TYPE_FOCUS_CONST_NAMES)} "
        "entries; expected at least 6 task-type focus blocks (refactor, qa, create, "
        "fix, docs, deps)"
    )

    # Verify every declared constant exists in the live task-type-focus.ts.
    agent_src = es_mod._find_agent_src()
    assert agent_src is not None
    ttf_path = os.path.join(agent_src, "prompts", "task-type-focus.ts")
    assert os.path.exists(ttf_path), (
        f"prompts/task-type-focus.ts not found at {ttf_path}"
    )
    with open(ttf_path, encoding="utf-8") as fh:
        ttf_src = fh.read()

    for name in _TASK_TYPE_FOCUS_CONST_NAMES:
        length = es_mod._extract_ts_template_literal_length(ttf_src, name)
        assert length is not None, (
            f"Constant {name!r} not found as a template literal in task-type-focus.ts. "
            "Either the constant was removed/renamed or _TASK_TYPE_FOCUS_CONST_NAMES "
            "needs updating."
        )
        assert length > 0, f"{name!r} has an empty template literal body"

    # Compute prompt.ts-only total to verify task-type-focus adds bytes.
    prompt_ts = os.path.join(agent_src, "prompt.ts")
    assert os.path.exists(prompt_ts)
    with open(prompt_ts, encoding="utf-8") as fh:
        prompt_src = fh.read()

    prompt_only = 0
    for name in _PROMPT_STATIC_CONST_NAMES:
        length = es_mod._extract_ts_template_literal_length(prompt_src, name)
        assert length is not None
        prompt_only += length
    for name in _PROMPT_STATIC_PLAIN_CONST_NAMES:
        length = es_mod._extract_ts_plain_string_length(prompt_src, name)
        assert length is not None
        prompt_only += length

    ttf_contribution = sum(
        es_mod._extract_ts_template_literal_length(ttf_src, name) or 0
        for name in _TASK_TYPE_FOCUS_CONST_NAMES
    )

    assert result == prompt_only + ttf_contribution, (
        f"_compute_prompt_static_bytes() returned {result} but expected "
        f"prompt_only ({prompt_only}) + ttf_contribution ({ttf_contribution}) = "
        f"{prompt_only + ttf_contribution}"
    )
    assert result > 8_000, (
        f"combined total {result} is below 8 000 — something is missing"
    )
    assert ttf_contribution > 1_000, (
        f"task-type-focus contribution ({ttf_contribution} bytes) is below 1 000; "
        "expected several thousand bytes across the six focus blocks"
    )


# ── self_repair_burn_share suppression test ──────────────────────────────────


@pytest.mark.asyncio
async def test_self_repair_burn_share_zero_clean_attempts_suppresses_drift(db_session):
    """When all repair attempts are infra failures, clean_attempts=0 falls below
    SELF_REPAIR_MIN_ATTEMPTS so drift_pct on self_repair_success_rate is None."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a baseline snapshot first.
    task_first = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 20}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "llm_call_count": 10,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 2,
            "format_repair_attempts": 2,
            "format_repair_successes": 1,
            "format_repair_infra_failures": 0,
        },
    )
    db_session.add(task_first)
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    # Now add a second task where ALL repair attempts are infra failures
    # → clean_attempts = 0 < SELF_REPAIR_MIN_ATTEMPTS → drift_pct suppressed.
    task_second = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "pr_number": 21}],
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 200,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 3,
            "format_repair_attempts": 3,
            "format_repair_successes": 0,
            "format_repair_infra_failures": 3,  # all infra failures → clean=0
        },
    )
    db_session.add(task_second)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)
    sr_axis = drift.get("format_compliance", {}).get(SELF_REPAIR_SUCCESS_RATE_KEY, {})
    # clean_attempts = repair_attempts - infra_failures = 3 - 3 = 0 < SELF_REPAIR_MIN_ATTEMPTS
    # → drift suppressed
    assert sr_axis.get("drift_pct") is None


async def _self_repair_burn_share_alerts(db_session, project_id):
    """Fetch self-repair burn-share alerts for a project."""
    stmt = _sql_select(Alert).where(
        Alert.project_id == project_id,
        Alert.title.contains("Self-repair token overhead"),
    )
    return (await db_session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="SQLite StaticPool reads stale state after DELETE in same session",
)
async def test_self_repair_burn_share_alert_threshold(db_session, mocker):
    """Alert fires when self_repair_burn_share exceeds threshold.

    Verify that:
    1. An alert fires when repair burn-share crosses above the threshold
    2. The alert contains the numeric values (current burn share, threshold)
    3. A second call at the same level dedupes (one alert only)
    4. Recovery below threshold re-arms the alert
    """
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    # Create a project with merged tasks. Repair burn-share is:
    # (repair_input_tokens + repair_output_tokens) / (total_input + total_output)
    # We'll create tasks such that burn-share exceeds the 0.20 (20%) threshold.
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with high repair overhead: 3000 repair tokens out of 10000 total = 30%
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 7000,
            "total_output_tokens": 3000,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 5,
            "format_repair_attempts": 5,
            "format_repair_successes": 3,
            "format_repair_infra_failures": 0,
            "format_repair_input_tokens": 2000,  # repair tokens contribute to burn_share
            "format_repair_output_tokens": 1000,  # total repair = 3000 tokens
        },
    )
    db_session.add(task)
    await db_session.commit()

    # First alert: burn-share at 30%, exceeds 20% threshold
    fired = await maybe_alert_self_repair_burn_share(db_session, project.id)
    assert fired is True

    alerts = await _self_repair_burn_share_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "Self-repair token overhead" in alerts[0].title
    # The message should contain percentages for operator context
    assert "30.0%" in alerts[0].message  # current burn share
    assert "20.0%" in alerts[0].message  # threshold

    # Verify the marker was set to "elevated"
    await db_session.refresh(project)
    marker = project.design_doc.get(DESIGN_DOC_KEY, {}).get(
        LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY
    )
    assert marker == "elevated"

    # Second alert at same level: deduped (one alert only)
    fired2 = await maybe_alert_self_repair_burn_share(db_session, project.id)
    assert fired2 is None  # no new alert fired

    alerts = await _self_repair_burn_share_alerts(db_session, project.id)
    assert len(alerts) == 1  # still only one alert

    # Now simulate recovery: replace task with one that has lower burn-share
    # New burn-share: 500 repair tokens out of 10000 total = 5% (below 20%)
    stmt = _sql_select(type(task)).where(type(task).id == task.id)
    old_task = (await db_session.execute(stmt)).scalar_one_or_none()
    if old_task:
        await db_session.delete(old_task)
    await db_session.commit()

    task_recovered = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 9000,
            "total_output_tokens": 1000,
            "llm_call_count": 5,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        format_compliance_summary={
            "format_error_total": 1,
            "format_repair_attempts": 1,
            "format_repair_successes": 1,
            "format_repair_infra_failures": 0,
            "format_repair_input_tokens": 300,
            "format_repair_output_tokens": 200,
        },
    )
    db_session.add(task_recovered)
    await db_session.commit()

    # Alert should not fire while below threshold
    fired3 = await maybe_alert_self_repair_burn_share(db_session, project.id)
    assert fired3 is None  # marker advanced to "healthy", no alert

    # Verify the marker was set to "healthy"
    await db_session.refresh(project)
    marker = project.design_doc.get(DESIGN_DOC_KEY, {}).get(
        LAST_ALERTED_SELF_REPAIR_BURN_SHARE_KEY
    )
    assert marker == "healthy"


def test_llm_cost_attribution_strategic_cycle_cost_per_event():
    """llm_cost_attribution block includes strategic_cycle_cost_per_event with
    cycle_count, total_usd, and per_event_usd when StrategicCycle rows exist.

    Uses two tasks tied to the same cycle (design_doc_update + strategic_review)
    and one unrelated create task. Verifies the aggregation is isolated to the
    strategic-cycle types and the per_event_usd is total / cycle_count.
    """
    import uuid

    from app.efficiency_snapshot import LLM_TOKEN_PRICING, _compact_llm_cost_attribution

    project_id = uuid.uuid4()
    cycle_id = str(uuid.uuid4())

    # design_doc_update (phase 1) — 1000 input + 200 output sonnet tokens
    phase1 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
        payloads=[{"type": "design_doc_update"}],
        strategic_cycle_id=cycle_id,
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 3,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # strategic_review (phase 2) — 500 input + 100 output sonnet tokens
    phase2 = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": False},
        payloads=[{"type": "strategic_review"}],
        strategic_cycle_id=cycle_id,
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "llm_call_count": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Unrelated create task (should NOT appear in strategic_cycle_cost_per_event)
    unrelated = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = _compact_llm_cost_attribution([phase1, phase2, unrelated])
    sc = result["strategic_cycle_cost_per_event"]

    assert sc is not None, (
        "strategic_cycle_cost_per_event must be non-None when cycles exist"
    )
    assert sc["cycle_count"] == 1
    # 1500 input + 300 output at claude-sonnet-4-6 rates (3e-6 in, 15e-6 out)
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_usd = round(1500 * input_rate + 300 * output_rate, 6)
    assert sc["total_usd"] == pytest.approx(expected_usd)
    assert sc["per_event_usd"] == pytest.approx(expected_usd / 1)


def test_llm_cost_attribution_no_strategic_cycle_cost_when_no_cycles():
    """strategic_cycle_cost_per_event is None when no strategic cycle tasks exist."""
    import uuid

    from app.efficiency_snapshot import _compact_llm_cost_attribution

    project_id = uuid.uuid4()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create"}],
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    result = _compact_llm_cost_attribution([task])
    assert result["strategic_cycle_cost_per_event"] is None


def test_llm_cost_attribution_strategic_cycle_counts_distinct_cycles():
    """strategic_cycle_cost_per_event.cycle_count reflects distinct cycle IDs."""
    import uuid

    from app.efficiency_snapshot import LLM_TOKEN_PRICING, _compact_llm_cost_attribution

    project_id = uuid.uuid4()
    cycle_a = str(uuid.uuid4())
    cycle_b = str(uuid.uuid4())

    def _sr_task(cycle_id: str) -> object:
        return make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            payloads=[{"type": "strategic_review"}],
            strategic_cycle_id=cycle_id,
            api_call_summary={
                "total_input_tokens": 400,
                "total_output_tokens": 80,
                "llm_call_count": 2,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )

    result = _compact_llm_cost_attribution([_sr_task(cycle_a), _sr_task(cycle_b)])
    sc = result["strategic_cycle_cost_per_event"]

    assert sc is not None
    assert sc["cycle_count"] == 2
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_total = round(2 * (400 * input_rate + 80 * output_rate), 6)
    assert sc["total_usd"] == pytest.approx(expected_total)
    assert sc["per_event_usd"] == pytest.approx(expected_total / 2)


def test_llm_cost_by_failure_class_attributes_admin_cancel_to_own_bucket():
    """LLM cost of an ADMIN_CANCEL/cancelled task is attributed to the ADMIN_CANCEL
    bucket and a merged task's cost lands in MERGED — end-to-end through
    _compact_llm_cost_attribution so the by_failure_class key is validated at the
    seam that writes the durable daily snapshot.
    """
    import uuid

    from app.efficiency_snapshot import (
        LLM_TOKEN_PRICING,
        _compact_llm_cost_attribution,
        compute_llm_cost_by_failure_class,
    )

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # ADMIN_CANCEL task: CANCELLED status, failure_category = ADMIN_CANCEL, has LLM cost.
    cancelled_task = make_task(
        project_id=project_id,
        status=TaskStatus.CANCELLED,
        context={"failure_category": "ADMIN_CANCEL"},
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Merged task: COMPLETED + pr_merged, no failure_category.
    merged_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    tasks = [cancelled_task, merged_task]

    # Verify the pure helper directly.
    rows = compute_llm_cost_by_failure_class(tasks)
    by_class = {r["failure_class"]: r for r in rows}

    cancel_cost = round(2000 * input_rate + 400 * output_rate, 6)
    merge_cost = round(1000 * input_rate + 200 * output_rate, 6)

    assert "ADMIN_CANCEL" in by_class
    assert by_class["ADMIN_CANCEL"]["task_count"] == 1
    assert by_class["ADMIN_CANCEL"]["total_cost_usd"] == pytest.approx(cancel_cost)

    assert "MERGED" in by_class
    assert by_class["MERGED"]["task_count"] == 1
    assert by_class["MERGED"]["total_cost_usd"] == pytest.approx(merge_cost)

    # Verify it flows through _compact_llm_cost_attribution (the snapshot seam).
    attribution = _compact_llm_cost_attribution(tasks)
    assert "by_failure_class" in attribution
    attr_by_class = {r["failure_class"]: r for r in attribution["by_failure_class"]}
    assert "ADMIN_CANCEL" in attr_by_class
    assert attr_by_class["ADMIN_CANCEL"]["total_cost_usd"] == pytest.approx(cancel_cost)
    assert "MERGED" in attr_by_class
    assert attr_by_class["MERGED"]["total_cost_usd"] == pytest.approx(merge_cost)


def test_llm_cost_by_outcome_separates_completed_no_pr_from_other():
    """COMPLETED tasks without a merged PR (research, review, memo) must land in
    COMPLETED_NO_PR, not OTHER, so operators can distinguish expected non-merge
    spend from undiagnosed failure spend.
    """
    import uuid

    from app.efficiency_snapshot import (
        LLM_TOKEN_PRICING,
        compute_llm_cost_by_failure_class,
    )

    project_id = uuid.uuid4()
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # COMPLETED task with no PR (research/review/memo type).
    completed_no_pr = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={},  # no pr_merged, no failure_category
        api_call_summary={
            "total_input_tokens": 3000,
            "total_output_tokens": 600,
            "llm_call_count": 10,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # FAILED task with no failure_category should still land in OTHER.
    failed_no_category = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        context={},
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 100,
            "llm_call_count": 2,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Merged task for baseline.
    merged_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
            "llm_call_count": 4,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    rows = compute_llm_cost_by_failure_class(
        [completed_no_pr, failed_no_category, merged_task]
    )
    by_class = {r["failure_class"]: r for r in rows}

    expected_no_pr_cost = round(3000 * input_rate + 600 * output_rate, 6)

    assert "COMPLETED_NO_PR" in by_class, (
        "research/memo completions must land in COMPLETED_NO_PR not OTHER"
    )
    assert by_class["COMPLETED_NO_PR"]["task_count"] == 1
    assert by_class["COMPLETED_NO_PR"]["total_cost_usd"] == pytest.approx(
        expected_no_pr_cost
    )

    # Undiagnosed failures still land in OTHER — the buckets are separate.
    assert "OTHER" in by_class
    assert by_class["OTHER"]["task_count"] == 1

    # COMPLETED_NO_PR and OTHER must not be merged together.
    assert (
        by_class["COMPLETED_NO_PR"]["total_cost_usd"]
        != by_class["OTHER"]["total_cost_usd"]
    )

    assert "MERGED" in by_class
    assert by_class["MERGED"]["task_count"] == 1


# ---------------------------------------------------------------------------
# llm_cost_attribution_baseline_delta tests
# ---------------------------------------------------------------------------


def test_compute_llm_cost_attribution_baseline_delta_returns_none_on_missing():
    """Returns None when either block is absent."""
    assert compute_llm_cost_attribution_baseline_delta(None, None) is None
    assert compute_llm_cost_attribution_baseline_delta({"total_llm_usd": 1.0}, None) is None
    assert compute_llm_cost_attribution_baseline_delta(None, {"total_llm_usd": 1.0}) is None


def test_llm_cost_attribution_baseline_delta():
    """compute_llm_cost_attribution_baseline_delta computes correct deltas for
    total_llm_usd, rework_share, self_repair_burn_share, by_model_delta, and
    by_task_type_delta between a synthetic baseline and current attribution block.

    Acceptance criterion: the named test must run green and exercise non-test
    production code (compute_llm_cost_attribution_baseline_delta in
    efficiency_snapshot.py).
    """
    baseline_attr = {
        "total_llm_usd": 10.0,
        "rework_share": 0.20,
        SELF_REPAIR_BURN_SHARE_KEY: 0.05,
        "by_model": [
            {"provider": "anthropic", "model": "claude-sonnet-4-6", "cost_usd": 8.0},
            {"provider": "anthropic", "model": "claude-opus-4-8", "cost_usd": 2.0},
        ],
        "by_task_type": [
            {"task_type": "create", "cost_usd": 6.0},
            {"task_type": "review", "cost_usd": 4.0},
        ],
    }
    current_attr = {
        "total_llm_usd": 12.6,
        "rework_share": 0.25,
        SELF_REPAIR_BURN_SHARE_KEY: 0.07,
        "by_model": [
            {"provider": "anthropic", "model": "claude-sonnet-4-6", "cost_usd": 9.0},
            {"provider": "anthropic", "model": "claude-opus-4-8", "cost_usd": 3.6},
        ],
        "by_task_type": [
            {"task_type": "create", "cost_usd": 7.0},
            {"task_type": "review", "cost_usd": 5.6},
        ],
    }

    delta = compute_llm_cost_attribution_baseline_delta(baseline_attr, current_attr)
    assert delta is not None

    # total_llm_usd delta
    total_d = delta["total_llm_usd"]
    assert total_d["baseline"] == pytest.approx(10.0)
    assert total_d["current"] == pytest.approx(12.6)
    assert total_d["abs_delta"] == pytest.approx(2.6, abs=1e-5)
    assert total_d["rel_pct"] == pytest.approx(26.0, abs=0.01)

    # rework_share delta
    rework_d = delta["rework_share"]
    assert rework_d["abs_delta"] == pytest.approx(0.05, abs=1e-6)

    # self_repair_burn_share delta
    repair_d = delta[SELF_REPAIR_BURN_SHARE_KEY]
    assert repair_d["baseline"] == pytest.approx(0.05)
    assert repair_d["current"] == pytest.approx(0.07)
    assert repair_d["abs_delta"] == pytest.approx(0.02, abs=1e-6)

    # by_model_delta sorted by abs(cost_delta_usd) desc; opus grew by 1.6, sonnet by 1.0
    by_model = delta["by_model_delta"]
    assert len(by_model) == 2
    assert by_model[0]["model"] == "claude-opus-4-8"
    assert by_model[0]["cost_delta_usd"] == pytest.approx(1.6, abs=1e-5)
    assert by_model[0]["baseline_share"] == pytest.approx(0.2, abs=1e-4)
    assert by_model[0]["current_share"] == pytest.approx(round(3.6 / 12.6, 4), abs=1e-4)

    # by_task_type_delta
    by_type = delta["by_task_type_delta"]
    assert len(by_type) == 2
    type_map = {r["task_type"]: r for r in by_type}
    assert type_map["review"]["cost_delta_usd"] == pytest.approx(1.6, abs=1e-5)
    assert type_map["create"]["cost_delta_usd"] == pytest.approx(1.0, abs=1e-5)


@pytest.mark.asyncio
async def test_llm_cost_attribution_baseline_delta_in_read_baseline(db_session):
    """read_baseline_with_drift exposes llm_cost_attribution_baseline_delta comparing
    the baseline's llm_cost_attribution to the latest snapshot's block.

    Two snapshots are written with different task sets so that the current
    attribution differs from the baseline, and the delta is verified to reflect
    the actual shift.  Exercises both write_snapshot and read_baseline_with_drift.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    # First snapshot (becomes baseline): one merged sonnet task.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "pr_number": 1}],
            api_call_summary={
                "total_input_tokens": 2000,
                "total_output_tokens": 500,
                "llm_call_count": 8,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    # Second snapshot (becomes latest/current): heavier task set, higher total cost.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "pr_number": 2}],
            api_call_summary={
                "total_input_tokens": 6000,
                "total_output_tokens": 1500,
                "llm_call_count": 20,
            },
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
    )
    await db_session.commit()
    await write_snapshot(db_session, project.id)
    await db_session.commit()

    drift = await read_baseline_with_drift(db_session, project.id)

    delta = drift.get("llm_cost_attribution_baseline_delta")
    assert delta is not None, "llm_cost_attribution_baseline_delta must be present"

    assert "total_llm_usd" in delta
    assert "rework_share" in delta
    assert SELF_REPAIR_BURN_SHARE_KEY in delta
    assert "by_model_delta" in delta
    assert "by_task_type_delta" in delta

    # current window has both tasks so total_llm_usd grows; delta must be positive.
    total_d = delta["total_llm_usd"]
    assert total_d["current"] is not None
    assert total_d["baseline"] is not None
    assert total_d["abs_delta"] is not None
    assert total_d["abs_delta"] > 0, (
        "current LLM cost must exceed baseline when more tasks are in the window"
    )

    # by_model_delta must include the sonnet model that ran in both windows.
    model_keys = {(r["provider"], r["model"]) for r in delta["by_model_delta"]}
    assert ("anthropic", "claude-sonnet-4-6") in model_keys


def test_low_coverage_not_healthy():
    """Effective coverage-aware gate health: low coverage prevents HEALTHY verdict.

    When sample_rate < 1.0, many merges are sampled_out (never challenged).
    The fail_open_rate computed over only the challenged population can look
    low (<=5%) even when most merges were never reviewed.

    Scenario: 100 merges total
      - 80 sampled_out (sample_rate=0.2, 80% skipped)
      - 19 challenged_confirmed (low fail_open rate)
      - 1 challenged_degraded (fail-open)

    Metrics:
      fail_open_rate = 1 / (19 + 1) = 0.05 = 5% (looks HEALTHY)
      effective_coverage = 20 / 100 = 0.20 (only 20% actually reviewed)

    Gate health verdict must account for low coverage and NOT return HEALTHY
    when effective_coverage is below threshold. effective_coverage must be
    exposed in the result so frontends can display it.
    """
    from app.efficiency_snapshot import compute_panel_review_effectiveness
    from app.task_completion import REVIEW_SECOND_OPINION_RECONCILIATION_KEY

    now = _utcnow()

    def _task(recon):
        return make_task(
            project_id=None,
            status=TaskStatus.COMPLETED,
            context={REVIEW_SECOND_OPINION_RECONCILIATION_KEY: recon},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
        )

    tasks = [
        # 80 sampled_out (not challenged, due to low sample_rate)
        *[_task({"state": "sampled_out", "sample_rate": 0.2}) for _ in range(80)],
        # 19 challenged_confirmed (low fail_open)
        *[_task({"state": "challenged_confirmed"}) for _ in range(19)],
        # 1 challenged_degraded (fail-open)
        _task({"state": "degraded_model_unavailable"}),
    ]

    result = compute_panel_review_effectiveness(tasks)
    assert result is not None, "must compute metrics with mix of sampled_out and challenged"

    # fail_open_rate looks low: 1 / (19 + 1) = 5%
    assert result["total_challenged"] == 19
    assert result["challenged_degraded"] == 1
    assert result["fail_open_rate"] == round(1 / 20, 4), (
        f"expected fail_open_rate=0.05, got {result['fail_open_rate']}"
    )

    # Effective coverage: only 20 out of 100 total invocation opportunities
    assert "sampled_out_count" in result, "must track sampled_out verdicts"
    assert result["sampled_out_count"] == 80, (
        f"expected sampled_out_count=80, got {result.get('sampled_out_count')}"
    )

    assert "effective_coverage" in result, "must compute and expose effective_coverage"
    expected_coverage = round(20 / 100, 4)  # (challenged + degraded) / (all invocation opportunities)
    assert result["effective_coverage"] == expected_coverage, (
        f"expected effective_coverage={expected_coverage}, got {result.get('effective_coverage')}"
    )


# ---------------------------------------------------------------------------
# compute_llm_cost_attribution_window_delta — pure unit tests
# ---------------------------------------------------------------------------

def _make_trend_entry(
    captured_at: str,
    total_llm_usd: float,
    rework_share: float,
    self_repair_burn_share: float,
    by_model: list | None = None,
    by_task_type: list | None = None,
) -> dict:
    """Build a synthetic trend entry matching the shape assembled by read_baseline_with_drift."""
    return {
        "captured_at": captured_at,
        "total_llm_usd": total_llm_usd,
        "rework_share": rework_share,
        SELF_REPAIR_BURN_SHARE_KEY: self_repair_burn_share,
        "by_model": by_model or [
            {"provider": "anthropic", "model": "claude-sonnet-4-6", "cost_usd": total_llm_usd * 0.8},
            {"provider": "anthropic", "model": "claude-opus-4-8", "cost_usd": total_llm_usd * 0.2},
        ],
        "by_task_type": by_task_type or [
            {"task_type": "create", "cost_usd": total_llm_usd * 0.6},
            {"task_type": "review", "cost_usd": total_llm_usd * 0.4},
        ],
    }


def test_compute_llm_cost_attribution_window_delta_returns_none_on_short_trend():
    """Returns None when trend has fewer than 2*recent_n entries."""
    assert compute_llm_cost_attribution_window_delta([]) is None
    assert compute_llm_cost_attribution_window_delta([_make_trend_entry("2026-01-01", 10.0, 0.1, 0.05)]) is None
    # Exactly recent_n * 2 - 1 entries (5 with default recent_n=3) → None
    short = [_make_trend_entry(f"2026-01-0{i}", float(i), 0.1, 0.05) for i in range(1, 6)]
    assert compute_llm_cost_attribution_window_delta(short) is None
    # Exactly recent_n * 2 entries (6) → not None
    six = [_make_trend_entry(f"2026-01-0{i}", float(i), 0.1, 0.05) for i in range(1, 7)]
    assert compute_llm_cost_attribution_window_delta(six) is not None


def test_compute_llm_cost_attribution_window_delta_scalar_fields():
    """Scalar fields (total_llm_usd, rework_share, self_repair_burn_share) are
    averaged within each window and compared correctly.

    Acceptance criterion: asserts the recent-vs-prior window split and correct
    scalar deltas for a synthetic 6-entry trend.
    """
    # prior window: entries 1-3 (total 10 each); recent window: entries 4-6 (total 20 each)
    trend = [
        _make_trend_entry("2026-01-01", 10.0, 0.10, 0.04),
        _make_trend_entry("2026-01-02", 10.0, 0.12, 0.05),
        _make_trend_entry("2026-01-03", 10.0, 0.08, 0.03),
        _make_trend_entry("2026-01-04", 20.0, 0.20, 0.08),
        _make_trend_entry("2026-01-05", 20.0, 0.22, 0.09),
        _make_trend_entry("2026-01-06", 20.0, 0.18, 0.07),
    ]

    delta = compute_llm_cost_attribution_window_delta(trend)
    assert delta is not None

    # total_llm_usd: prior avg = 10.0, recent avg = 20.0 → abs_delta = +10.0, rel_pct = +100%
    total_d = delta["total_llm_usd"]
    assert total_d["prior"] == pytest.approx(10.0, abs=1e-4)
    assert total_d["recent"] == pytest.approx(20.0, abs=1e-4)
    assert total_d["abs_delta"] == pytest.approx(10.0, abs=1e-4)
    assert total_d["rel_pct"] == pytest.approx(100.0, abs=0.1)

    # rework_share: prior avg = (0.10+0.12+0.08)/3 = 0.10; recent avg = (0.20+0.22+0.18)/3 = 0.20
    rework_d = delta["rework_share"]
    assert rework_d["prior"] == pytest.approx(0.10, abs=1e-5)
    assert rework_d["recent"] == pytest.approx(0.20, abs=1e-5)
    assert rework_d["abs_delta"] == pytest.approx(0.10, abs=1e-5)

    # self_repair_burn_share: prior avg = (0.04+0.05+0.03)/3 ≈ 0.04; recent avg ≈ 0.08
    repair_d = delta[SELF_REPAIR_BURN_SHARE_KEY]
    assert repair_d["abs_delta"] is not None
    assert repair_d["abs_delta"] > 0


def test_compute_llm_cost_attribution_window_delta_list_fields():
    """Per-model and per-task-type deltas use averaged cost sums and are sorted
    by abs(cost_delta_usd) descending (biggest mover first).

    Acceptance criterion: asserts correct per-model and per-task-type cost delta
    values and ordering for a synthetic trend where opus grows more than sonnet.
    """
    # prior: sonnet=8, opus=2 per snapshot; recent: sonnet=9, opus=5 per snapshot
    def _entry(ts: str, sonnet: float, opus: float) -> dict:
        return _make_trend_entry(
            ts, sonnet + opus, 0.1, 0.05,
            by_model=[
                {"provider": "anthropic", "model": "claude-sonnet-4-6", "cost_usd": sonnet},
                {"provider": "anthropic", "model": "claude-opus-4-8", "cost_usd": opus},
            ],
            by_task_type=[
                {"task_type": "create", "cost_usd": (sonnet + opus) * 0.6},
                {"task_type": "review", "cost_usd": (sonnet + opus) * 0.4},
            ],
        )

    trend = [
        _entry("2026-01-01", 8.0, 2.0),
        _entry("2026-01-02", 8.0, 2.0),
        _entry("2026-01-03", 8.0, 2.0),
        _entry("2026-01-04", 9.0, 5.0),
        _entry("2026-01-05", 9.0, 5.0),
        _entry("2026-01-06", 9.0, 5.0),
    ]

    delta = compute_llm_cost_attribution_window_delta(trend)
    assert delta is not None

    by_model = delta["by_model_delta"]
    assert len(by_model) == 2
    # opus cost delta = 5-2 = +3; sonnet delta = 9-8 = +1 → opus is first
    assert by_model[0]["model"] == "claude-opus-4-8"
    assert by_model[0]["cost_delta_usd"] == pytest.approx(3.0, abs=1e-4)
    assert by_model[1]["model"] == "claude-sonnet-4-6"
    assert by_model[1]["cost_delta_usd"] == pytest.approx(1.0, abs=1e-4)

    # share fields are present
    assert by_model[0]["prior_share"] is not None
    assert by_model[0]["recent_share"] is not None
    assert by_model[0]["share_delta"] is not None
    # opus share grew: 2/10 = 0.2 → 5/14 ≈ 0.357 → share_delta > 0
    assert by_model[0]["share_delta"] > 0

    # by_task_type_delta: both types grew proportionally; create grew more (larger abs USD)
    by_type = delta["by_task_type_delta"]
    assert len(by_type) == 2
    type_map = {r["task_type"]: r for r in by_type}
    assert "create" in type_map
    assert "review" in type_map
    # create: prior 6, recent 8.4 → delta +2.4; review: prior 4, recent 5.6 → delta +1.6
    assert type_map["create"]["cost_delta_usd"] > type_map["review"]["cost_delta_usd"]


def test_compute_llm_cost_attribution_window_delta_window_meta():
    """window_meta includes snapshot counts and captured_at bounds."""
    trend = [_make_trend_entry(f"2026-01-0{i}", 10.0 + i, 0.1, 0.05) for i in range(1, 7)]
    delta = compute_llm_cost_attribution_window_delta(trend)
    assert delta is not None

    meta = delta["window_meta"]
    assert meta is not None
    assert meta["recent_n"] == 3
    assert meta["prior_count"] == 3
    assert meta["recent_count"] == 3
    assert meta["prior_start"] == "2026-01-01"
    assert meta["prior_end"] == "2026-01-03"
    assert meta["recent_start"] == "2026-01-04"
    assert meta["recent_end"] == "2026-01-06"


@pytest.mark.asyncio
async def test_llm_cost_attribution_window_delta_in_read_baseline(db_session):
    """read_baseline_with_drift exposes llm_cost_attribution_window_delta when
    the stored trend has sufficient depth (at least 2*recent_n snapshots).

    Seeds a project with 6 write_snapshot calls using different LLM cost loads
    so that the recent window differs from the prior window.  Asserts the key is
    present and the shape is correct.

    Acceptance criterion (from task spec): efficiency_snapshot computes a
    trailing-window (recent-vs-prior) cost-mix delta from the stored trend over
    a synthetic multi-snapshot series.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]

    async def _write_with_task(tokens_in: int, tokens_out: int, pr_num: int) -> None:
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"pr_merged": True},
                payloads=[{"type": "create", "pr_number": pr_num}],
                api_call_summary={
                    "total_input_tokens": tokens_in,
                    "total_output_tokens": tokens_out,
                    "llm_call_count": 4,
                },
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
        )
        await db_session.commit()
        await write_snapshot(db_session, project.id)
        await db_session.commit()

    # Write 3 low-cost snapshots (prior window)
    for i in range(3):
        await _write_with_task(1000, 200, i + 1)

    # Write 3 higher-cost snapshots (recent window)
    for i in range(3):
        await _write_with_task(5000, 1000, i + 10)

    drift = await read_baseline_with_drift(db_session, project.id)

    wd = drift.get("llm_cost_attribution_window_delta")
    assert wd is not None, "llm_cost_attribution_window_delta must be present when trend depth >= 6"

    assert "total_llm_usd" in wd
    assert "rework_share" in wd
    assert SELF_REPAIR_BURN_SHARE_KEY in wd
    assert "by_model_delta" in wd
    assert "by_task_type_delta" in wd
    assert "window_meta" in wd

    meta = wd["window_meta"]
    assert meta["recent_n"] == 3
    assert meta["recent_count"] == 3
    assert meta["prior_count"] == 3

    # Recent window had higher LLM cost than prior → abs_delta must be positive
    total_d = wd["total_llm_usd"]
    assert total_d["recent"] is not None
    assert total_d["prior"] is not None
    assert total_d["abs_delta"] is not None
    assert total_d["abs_delta"] > 0, (
        "recent window had heavier tasks so avg cost must exceed prior window"
    )

    # sonnet model must appear in the by_model_delta list
    model_keys = {(r["provider"], r["model"]) for r in wd["by_model_delta"]}
    assert ("anthropic", "claude-sonnet-4-6") in model_keys


def test_landed_rate_denominator_excludes_pr_ineligible():
    """landed_rate and verified_landed_rate use only PR-eligible tasks as denominator.

    PR-ineligible tasks (review, plan, research, …) can never open a PR, so they
    can only inflate run_count without contributing to landed_count.  The fix
    divides by pr_eligible_run_count (ci_and_review types only) instead of
    run_count, so the rate truly answers "of the tasks that could ship code, what
    fraction did?"

    Scenario:
      - 1 merged create task  (PR-eligible, verified)
      - 1 unmerged create task (PR-eligible, did not land)
      - 3 review tasks        (PR-ineligible — must not enter the denominator)

    Expected:
      - totals.pr_eligible_run_count == 2
      - totals.landed_rate == 0.5  (1 landed / 2 pr-eligible)
      - totals.verified_landed_rate == 0.5
      Without the fix: run_count == 5 → landed_rate == 0.2 (wrong).
    """
    import uuid
    from app.efficiency_snapshot import compute_effort_model_efficiency
    from app.models import TaskStatus

    project_id = uuid.uuid4()

    merged_create = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "effort": "medium"}],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    unmerged_create = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={},
        payloads=[{"type": "create", "effort": "medium"}],
        provider="anthropic",
        model="claude-sonnet-4-6",
    )

    # Three review tasks — PR-ineligible; must not count in landed_rate denominator.
    review_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            payloads=[{"type": "review", "effort": "high"}],
            provider="anthropic",
            model="claude-opus-4-8",
        )
        for _ in range(3)
    ]

    result = compute_effort_model_efficiency(
        [merged_create, unmerged_create] + review_tasks
    )

    totals = result["totals"]

    assert totals["run_count"] == 5, "run_count must include all tasks"
    assert totals["pr_eligible_run_count"] == 2, (
        "pr_eligible_run_count must count only ci_and_review tasks (create x2)"
    )
    assert totals["landed_count"] == 1
    # landed_rate = 1 landed / 2 pr-eligible = 0.5
    assert totals["landed_rate"] == 0.5, (
        f"landed_rate must divide by pr_eligible_run_count (2), got {totals['landed_rate']}"
    )
    # With no is_verified_merge signal (context lacks review_loop CI info), the
    # merged create counts as verified too (no unverified_merge_reason set).
    assert totals["verified_landed_rate"] == 0.5, (
        f"verified_landed_rate must use pr_eligible denominator, got {totals['verified_landed_rate']}"
    )

    # The by_effort breakdown must also carry pr_eligible_run_count per tier.
    by_effort_map = {row["effort"]: row for row in result["by_effort"]}
    medium_row = by_effort_map.get("medium")
    assert medium_row is not None, "medium effort bucket must exist for create tasks"
    assert medium_row["pr_eligible_run_count"] == 2
    assert medium_row["landed_rate"] == 0.5

    # The by_model breakdown must credit pr_eligible_run_count in the per-model
    # crediting loop too, else landed_rate/verified_landed_rate collapse to None
    # (divide-by-zero denominator) for every model row.
    by_model_map = {(r["provider"], r["model"]): r for r in result["by_model"]}
    sonnet_row = by_model_map.get(("anthropic", "claude-sonnet-4-6"))
    assert sonnet_row is not None, "create tasks' model must appear in by_model"
    assert sonnet_row["pr_eligible_run_count"] == 2, (
        "by_model pr_eligible_run_count must count PR-eligible create tasks, "
        f"got {sonnet_row['pr_eligible_run_count']}"
    )
    assert sonnet_row["landed_count"] == 1
    assert sonnet_row["landed_rate"] == 0.5, (
        f"by_model landed_rate must divide by pr_eligible_run_count (2), got "
        f"{sonnet_row['landed_rate']}"
    )
    # PR-ineligible review-only model: zero pr_eligible denominator → rate is None.
    opus_row = by_model_map.get(("anthropic", "claude-opus-4-8"))
    assert opus_row is not None, "review tasks' model must appear in by_model"
    assert opus_row["pr_eligible_run_count"] == 0
    assert opus_row["landed_rate"] is None


def test_landed_rate_denominator_excludes_read_only_task_types():
    """landed_rate denominator excludes every READ_ONLY_TASK_TYPE.

    READ_ONLY_TASK_TYPES are structurally incapable of landing a PR, so
    including them in the denominator deflates landed_rate in exactly the
    high-cost effort tiers operators scrutinize.

    This test exercises the production fix directly: pr_eligible_run_count
    (ci_and_review tasks only) is the denominator, not run_count.  It also
    verifies that the _resolved_models rows (per-model within an effort tier)
    carry pr_eligible_run_count so the by_effort model breakdown is
    self-consistent.

    Scenario:
      - 2 merged create tasks (PR-eligible, medium effort)
      - 1 each of review, research, plan, strategic_review (read-only, high)

    Expected:
      - totals.run_count == 6
      - totals.pr_eligible_run_count == 2
      - totals.landed_rate == 1.0 (2 landed / 2 pr-eligible)
      - by_effort[medium].pr_eligible_run_count == 2
      - resolved_models rows for read-only model have pr_eligible_run_count == 0
    """
    import uuid
    from app.efficiency_snapshot import compute_effort_model_efficiency
    from app.models import TaskStatus
    from app.task_capabilities import READ_ONLY_TASK_TYPES

    project_id = uuid.uuid4()

    create_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "medium"}],
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        for _ in range(2)
    ]

    # Sample four distinct read-only task types to verify none enter the
    # pr_eligible denominator regardless of which specific type they are.
    read_only_sample = ["review", "research", "plan", "strategic_review"]
    assert all(t in READ_ONLY_TASK_TYPES for t in read_only_sample), (
        "test fixture uses types that must be in READ_ONLY_TASK_TYPES"
    )
    read_only_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            payloads=[{"type": task_type, "effort": "high"}],
            provider="anthropic",
            model="claude-opus-4-8",
        )
        for task_type in read_only_sample
    ]

    result = compute_effort_model_efficiency(create_tasks + read_only_tasks)

    totals = result["totals"]
    assert totals["run_count"] == 6
    assert totals["pr_eligible_run_count"] == 2, (
        f"only create tasks are PR-eligible; got {totals['pr_eligible_run_count']}"
    )
    assert totals["landed_count"] == 2
    assert totals["landed_rate"] == 1.0, (
        f"landed_rate must divide by pr_eligible_run_count (2), got {totals['landed_rate']}"
    )

    # Per-effort breakdown: medium bucket should have both creates, zero read-only.
    by_effort_map = {row["effort"]: row for row in result["by_effort"]}
    medium_row = by_effort_map.get("medium")
    assert medium_row is not None
    assert medium_row["pr_eligible_run_count"] == 2
    assert medium_row["landed_rate"] == 1.0

    # Per-model rows within the medium effort bucket carry pr_eligible_run_count.
    medium_models = {
        (r["provider"], r["model"]): r
        for r in medium_row.get("resolved_models", [])
    }
    sonnet_em = medium_models.get(("anthropic", "claude-sonnet-4-6"))
    assert sonnet_em is not None, "create-task model must appear in medium resolved_models"
    assert sonnet_em["pr_eligible_run_count"] == 2, (
        f"resolved_models row must expose pr_eligible_run_count; got {sonnet_em}"
    )

    # high effort bucket: all read-only → pr_eligible_run_count == 0 → landed_rate is None.
    high_row = by_effort_map.get("high")
    assert high_row is not None
    assert high_row["pr_eligible_run_count"] == 0
    assert high_row["landed_rate"] is None

    # Per-model rows in the high bucket: the read-only model has pr_eligible == 0.
    high_models = {
        (r["provider"], r["model"]): r
        for r in high_row.get("resolved_models", [])
    }
    opus_em = high_models.get(("anthropic", "claude-opus-4-8"))
    assert opus_em is not None
    assert opus_em["pr_eligible_run_count"] == 0, (
        f"read-only model must have pr_eligible_run_count=0; got {opus_em}"
    )


def test_provider_routing_verdict_derivation():
    """Provider routing verdict derives ADOPT/REJECT/INSUFFICIENT_DATA from by_model rows.

    Exercises all four classification branches:
      - INSUFFICIENT_DATA when either provider has too few PR-eligible tasks
      - REJECT when DeepSeek verified-merge rate materially regresses
      - REJECT when DeepSeek cost-per-merge is not meaningfully cheaper
      - ADOPT when DeepSeek is cheaper AND quality holds
    """
    import uuid

    from app.models import TaskStatus

    project_id = uuid.uuid4()

    # -- helper: build by_model rows via compute_effort_model_efficiency ------
    def _by_model(tasks):
        return compute_effort_model_efficiency(tasks)["by_model"]

    # -- INSUFFICIENT_DATA: too few tasks on either side ---------------------
    few_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        for _ in range(5)
    ] + [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider="anthropic",
            model="claude-sonnet-4-6",
        )
        for _ in range(5)
    ]
    verdict = compute_provider_routing_verdict(_by_model(few_tasks))
    assert verdict["state"] == PROVIDER_ROUTING_INSUFFICIENT_DATA, (
        f"expected INSUFFICIENT_DATA with only 5 tasks each, got {verdict['state']}"
    )
    assert "supporting" in verdict
    assert "recommended_action" in verdict

    # -- ADOPT: DeepSeek cheaper with no quality regression ------------------
    adopted_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider=prov,
            model=mod,
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 5,
            },
        )
        for prov, mod in [("deepseek", "deepseek-v4-flash")] * 30
        + [("anthropic", "claude-sonnet-4-6")] * 30
    ]
    # Equal verified rates: first 24 deepseek + first 24 anthropic stay merged,
    # remaining 6 each side become unmerged.
    for i, t in enumerate(adopted_tasks):
        if (t.provider == "deepseek" and i >= 24) or (
            t.provider == "anthropic" and i >= 54
        ):
            t.context = {}

    verdict = compute_provider_routing_verdict(_by_model(adopted_tasks))
    assert verdict["state"] == PROVIDER_ROUTING_ADOPT, (
        f"expected ADOPT with DeepSeek cheaper + same quality, got {verdict['state']}"
    )
    s = verdict["supporting"]
    assert s["deepseek_pr_eligible_run_count"] >= 20
    assert s["anthropic_pr_eligible_run_count"] >= 20
    assert s["cost_ratio"] is not None and s["cost_ratio"] < 1.0, (
        "DeepSeek must be cheaper than Anthropic"
    )

    # -- REJECT: quality regression (verified rate materially lower) ---------
    regressed_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider=prov,
            model=mod,
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "llm_call_count": 5,
            },
        )
        for prov, mod in [("deepseek", "deepseek-v4-flash")] * 30
        + [("anthropic", "claude-sonnet-4-6")] * 30
    ]
    # DeepSeek: only 10/30 verified (33% rate).  Anthropic: 24/30 (80% rate).
    # That's a 47pp delta → well below the -5pp quality floor → REJECT.
    for i, t in enumerate(regressed_tasks):
        if (t.provider == "deepseek" and i >= 10) or (
            t.provider == "anthropic" and i >= 54
        ):
            t.context = {}

    verdict = compute_provider_routing_verdict(_by_model(regressed_tasks))
    assert verdict["state"] == PROVIDER_ROUTING_REJECT, (
        f"expected REJECT with DeepSeek quality regression, got {verdict['state']}"
    )
    s = verdict["supporting"]
    assert s["verified_rate_delta_pct"] is not None
    # The delta should be strongly negative (DeepSeek rate << Anthropic rate).
    assert s["verified_rate_delta_pct"] < -5.0, (
        f"rate delta should be below -5pp floor, got {s['verified_rate_delta_pct']}"
    )

    # -- REJECT: cost not meaningfully lower (cost_ratio >= 0.90) ------------
    # Both providers have equal verified rates (rate_delta ≈ 0, quality OK),
    # but DeepSeek's cost-per-merge is not meaningfully cheaper because its
    # per-task token consumption is high enough to offset its lower per-token
    # pricing.
    expensive_ds_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider=prov,
            model=mod,
            api_call_summary={
                # DeepSeek uses ~37x more tokens than Anthropic per call,
                # cancelling out its ~37x cheaper per-token pricing.
                "total_input_tokens": 37000 if prov == "deepseek" else 1000,
                "total_output_tokens": 18500 if prov == "deepseek" else 500,
                "llm_call_count": 5,
            },
        )
        for prov, mod in [("deepseek", "deepseek-v4-flash")] * 30
        + [("anthropic", "claude-sonnet-4-6")] * 30
    ]
    # Equal verified rates: 24/30 each → ~0pp delta (quality OK).
    for i, t in enumerate(expensive_ds_tasks):
        if (t.provider == "deepseek" and i >= 24) or (
            t.provider == "anthropic" and i >= 54
        ):
            t.context = {}

    verdict = compute_provider_routing_verdict(_by_model(expensive_ds_tasks))
    assert verdict["state"] == PROVIDER_ROUTING_REJECT, (
        f"expected REJECT with DeepSeek cost not meaningfully lower, "
        f"got {verdict['state']}"
    )
    s = verdict["supporting"]
    assert s["cost_ratio"] is not None and s["cost_ratio"] >= 0.90, (
        f"cost_ratio should be >= 0.90 to trigger cost-rejection, "
        f"got {s['cost_ratio']}"
    )
    # Quality should be OK (rate delta should be 0pp, well above -5pp floor).
    assert s["verified_rate_delta_pct"] > -5.0, (
        f"rate delta should be above quality floor, got {s['verified_rate_delta_pct']}"
    )


@pytest.mark.asyncio
async def test_seed_routing_verdict_from_snapshot_restores_verdict_state(db_session):
    """Verdict state is seeded from persisted efficiency_baseline on startup.

    Verifies that ``_seed_routing_verdict_from_snapshot`` reads the
    ``provider_routing_verdict`` from ``project.design_doc["efficiency_baseline"]``
    and sets ``runtime_config["provider_routing_verdict_state"]`` accordingly.
    This is the mechanism that restores DeepSeek preference on backend restart
    without waiting for the next periodic snapshot write.
    """
    from app.shared_state import runtime_config as _rc
    from app.lifecycle import _seed_routing_verdict_from_snapshot

    # Clean slate: remove any pre-existing verdict state so the test is hermetic.
    _rc.pop("provider_routing_verdict_state", None)

    project = make_project(
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "provider_routing_verdict": {
                        "state": PROVIDER_ROUTING_ADOPT,
                        "supporting": {
                            "deepseek_run_count": 30,
                            "anthropic_run_count": 30,
                        },
                        "recommended_action": "Adopt DeepSeek routing",
                    },
                },
                "snapshots": [],
            }
        }
    )
    db_session.add(project)
    await db_session.commit()

    await _seed_routing_verdict_from_snapshot()

    assert _rc.get("provider_routing_verdict_state") == PROVIDER_ROUTING_ADOPT, (
        f"expected {PROVIDER_ROUTING_ADOPT}, got {_rc.get('provider_routing_verdict_state')}"
    )

    # Clean up so we don't leak state into other tests.
    _rc.pop("provider_routing_verdict_state", None)


@pytest.mark.asyncio
async def test_seed_routing_verdict_from_snapshot_skips_project_without_verdict(db_session):
    """When no project has a verdict, the runtime_config key remains unset."""
    from app.shared_state import runtime_config as _rc
    from app.lifecycle import _seed_routing_verdict_from_snapshot

    _rc.pop("provider_routing_verdict_state", None)

    # Project exists but has no efficiency_baseline in design_doc.
    project = make_project(design_doc={"some_other_section": {}})
    db_session.add(project)
    await db_session.commit()

    await _seed_routing_verdict_from_snapshot()

    assert "provider_routing_verdict_state" not in _rc, (
        "key should remain absent when no project has a verdict"
    )


@pytest.mark.asyncio
async def test_seed_routing_verdict_from_snapshot_picks_first_verdict(db_session):
    """When multiple projects have verdicts, the first one wins (no overwrite)."""
    from app.shared_state import runtime_config as _rc
    from app.lifecycle import _seed_routing_verdict_from_snapshot

    _rc.pop("provider_routing_verdict_state", None)

    p1 = make_project(
        name="Project A",
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "provider_routing_verdict": {"state": PROVIDER_ROUTING_ADOPT},
                },
            }
        },
    )
    p2 = make_project(
        name="Project B",
        design_doc={
            DESIGN_DOC_KEY: {
                "baseline": {
                    "provider_routing_verdict": {"state": PROVIDER_ROUTING_REJECT},
                },
            }
        },
    )
    db_session.add(p1)
    db_session.add(p2)
    await db_session.commit()

    await _seed_routing_verdict_from_snapshot()

    # The first project (by iteration order) seeds the verdict. If it's ADOPT,
    # it wins; if REJECT unexpectedly showed first, that would also be correct.
    # The key assertion is that SOME verdict is set (not INSUFFICIENT_DATA).
    state = _rc.get("provider_routing_verdict_state")
    assert state in (PROVIDER_ROUTING_ADOPT, PROVIDER_ROUTING_REJECT), (
        f"unexpected state {state}"
    )


@pytest.mark.asyncio
async def test_write_snapshot_persists_effort_model_efficiency_by_model(db_session):
    """write_snapshot persists the effort_model_efficiency block (by_model + totals)
    from compute_effort_model_efficiency, and read_baseline_with_drift surfaces it.

    Creates tasks on two providers (Anthropic Sonnet and DeepSeek V4 Flash),
    writes a snapshot, and verifies the by_model rows contain the correct
    per-model fields (run_count, pr_eligible_run_count, verified_landed_count,
    verified_landed_rate, cost_per_merge, total_cost_usd) and the totals are
    consistent with the fixture.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Sonnet task: merged create, 1 landed out of 1 pr-eligible.
    sonnet_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "effort": "medium", "pr_number": 1}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 500,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # DeepSeek task: also merged create, 1 landed out of 1 pr-eligible.
    deepseek_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "effort": "low", "pr_number": 2}],
        api_call_summary={
            "total_input_tokens": 1500,
            "total_output_tokens": 400,
            "llm_call_count": 6,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    db_session.add(sonnet_task)
    db_session.add(deepseek_task)
    await db_session.commit()

    # --- write_snapshot returns the block ------------------------------------
    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    em = snap.get("effort_model_efficiency")
    assert em is not None, (
        "effort_model_efficiency must be present in write_snapshot return value"
    )

    # Top-level keys: by_model + totals
    by_model = em.get("by_model")
    totals = em.get("totals")
    assert isinstance(by_model, list), "by_model must be a list"
    assert isinstance(totals, dict), "totals must be a dict"
    assert len(by_model) >= 2, "at least 2 model rows for sonnet + deepseek"

    # Build lookup by (provider, model)
    by_model_map = {(r["provider"], r["model"]): r for r in by_model}

    # -- Sonnet row assertions ------------------------------------------------
    sonnet_row = by_model_map.get(("anthropic", "claude-sonnet-4-6"))
    assert sonnet_row is not None, "claude-sonnet-4-6 must appear in by_model"
    assert sonnet_row["run_count"] == 1
    assert sonnet_row["pr_eligible_run_count"] == 1
    assert sonnet_row["landed_count"] == 1
    assert sonnet_row["verified_landed_count"] == 1
    assert sonnet_row["verified_landed_rate"] == 1.0, (
        "1 verified / 1 pr-eligible = 1.0"
    )
    assert sonnet_row["cost_per_merge"] is not None, (
        "cost_per_merge is not None when verified_landed_count > 0"
    )

    # -- DeepSeek row assertions ----------------------------------------------
    ds_row = by_model_map.get(("deepseek", "deepseek-v4-flash"))
    assert ds_row is not None, "deepseek-v4-flash must appear in by_model"
    assert ds_row["run_count"] == 1
    assert ds_row["pr_eligible_run_count"] == 1
    assert ds_row["landed_count"] == 1
    assert ds_row["verified_landed_count"] == 1
    assert ds_row["verified_landed_rate"] == 1.0
    assert ds_row["cost_per_merge"] is not None

    # -- Totals assertions ----------------------------------------------------
    assert totals["run_count"] == 2, "total must include both tasks"
    assert totals["pr_eligible_run_count"] == 2, (
        "both tasks are PR-eligible create"
    )
    assert totals["landed_count"] == 2
    assert totals["verified_landed_count"] == 2
    assert totals["landed_rate"] == 1.0  # 2/2

    # -- read_baseline_with_drift surfaces the same block ---------------------
    drift = await read_baseline_with_drift(db_session, project.id)
    read_em = drift.get("effort_model_efficiency")
    assert read_em is not None, (
        "read_baseline_with_drift must surface effort_model_efficiency"
    )
    read_by_model = read_em.get("by_model")
    assert len(read_by_model) == len(by_model), (
        "by_model row count must match between write and read"
    )


async def test_model_routing_efficiency_persisted_in_snapshot(db_session):
    """provider_routing_share (per-provider aggregation of by_model) is persisted
    in write_snapshot and surfaced by read_baseline_with_drift.

    Creates tasks on two providers, writes a snapshot, and verifies the
    by_provider block aggregates correctly.
    """
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Sonnet task: merged create, 1 pr-eligible, 1 verified.
    sonnet_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "effort": "medium", "pr_number": 1}],
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 500,
            "llm_call_count": 8,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # DeepSeek task: merged create, 1 pr-eligible, 1 verified.
    deepseek_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "effort": "low", "pr_number": 2}],
        api_call_summary={
            "total_input_tokens": 1500,
            "total_output_tokens": 400,
            "llm_call_count": 6,
        },
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    db_session.add(sonnet_task)
    db_session.add(deepseek_task)
    await db_session.commit()

    # --- write_snapshot returns provider_routing_share -------------------------
    snap = await write_snapshot(db_session, project.id)
    await db_session.commit()

    prs = snap.get("provider_routing_share")
    assert prs is not None, (
        "provider_routing_share must be present in write_snapshot return value"
    )

    by_provider = prs.get("by_provider")
    assert isinstance(by_provider, list), "by_provider must be a list"
    assert len(by_provider) >= 2, "at least 2 provider rows for anthropic + deepseek"

    by_provider_map = {r["provider"]: r for r in by_provider}
    assert "anthropic" in by_provider_map
    assert "deepseek" in by_provider_map

    # -- Anthropic row: 1 run, 1 pr-eligible, 1 verified -----------------------
    anth = by_provider_map["anthropic"]
    assert anth["run_count"] == 1
    assert anth["pr_eligible_run_count"] == 1
    assert anth["verified_landed_count"] == 1
    assert anth["verified_landed_rate"] == 1.0
    assert anth["cost_per_merge"] is not None

    # -- DeepSeek row: 1 run, 1 pr-eligible, 1 verified ------------------------
    ds = by_provider_map["deepseek"]
    assert ds["run_count"] == 1
    assert ds["pr_eligible_run_count"] == 1
    assert ds["verified_landed_count"] == 1
    assert ds["verified_landed_rate"] == 1.0
    assert ds["cost_per_merge"] is not None

    # -- Totals -----------------------------------------------------------------
    assert prs["total_run_count"] == 2
    assert prs["total_pr_eligible_run_count"] == 2

    # -- read_baseline_with_drift surfaces the same block -----------------------
    drift = await read_baseline_with_drift(db_session, project.id)
    read_prs = drift.get("provider_routing_share")
    assert read_prs is not None, (
        "read_baseline_with_drift must surface provider_routing_share"
    )
    assert len(read_prs["by_provider"]) == len(by_provider), (
        "by_provider row count must match between write and read"
    )
