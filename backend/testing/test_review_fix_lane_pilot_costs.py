"""Focused tests for review-fix lane-pilot cost attribution."""

import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock

from app.improvement_lane_ops import create_lane
from app.models import ImprovementLane
from app.review_fix_lane_pilot import (
    LANE_CHAIN_ID_KEY,
    _SHADOW_ARM,
    compute_lane_pilot_aggregates,
    is_review_fix_lane_pilot_shadow_mode_active_for_project,
    record_lane_arm_assignment,
)
from app.capability_trial_verdict import (
    LANE_PILOT_ASSIGNMENT_KEY,
    evaluate_lane_pilot_verdict,
)
from app.models import TaskStatus
from testing.factories import make_project, make_task


async def _make_project(db_session, **project_kwargs):
    project = make_project(**project_kwargs)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_include_reserved_idle_and_exclude_shadow(
    db_session,
):
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-chain",
        goal="lane-chain",
        branch_name="lane-chain",
        base_branch="main",
        pilot_arm="lane",
    )
    lane.reserved_idle_cost_usd_total = 1.25

    shadow_lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="shadow-chain",
        goal="shadow-chain",
        branch_name="shadow-chain",
        base_branch="main",
        pilot_arm=None,
    )
    shadow_lane.reserved_idle_cost_usd_total = 99.0

    root_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
        },
        estimated_cost_usd=0.5,
    )
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "review",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )
    fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": True,
        },
        estimated_cost_usd=1.5,
    )
    control_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: "control", "pr_merged": True},
        estimated_cost_usd=4.0,
    )

    db_session.add_all(
        [lane, shadow_lane, root_task, review_task, fix_task, control_task]
    )
    await db_session.commit()
    await db_session.refresh(lane)
    await db_session.refresh(shadow_lane)

    lanes = (
        (
            await db_session.execute(
                select(ImprovementLane).where(ImprovementLane.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert {ln.id for ln in lanes} >= {lane.id, shadow_lane.id}

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    assert agg["n_lane"] == 1
    assert agg["n_control"] == 1
    assert agg["lane_runtime_cost_per_merged"] == pytest.approx(3.0)
    assert agg["lane_reserved_idle_cost_per_merged"] == pytest.approx(1.25)
    assert agg["lane_cost_per_merged"] == pytest.approx(4.25)
    assert agg["control_runtime_cost_per_merged"] == pytest.approx(4.0)
    assert agg["control_reserved_idle_cost_per_merged"] == pytest.approx(0.0)
    assert agg["control_cost_per_merged"] == pytest.approx(4.0)
    assert agg["lane_cost_per_merged"] < 100.0


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_include_failed_fix_task_cost(db_session):
    """Verify a FAILED fix task's cost is included in lane_cost_per_merged.

    Intent-to-treat semantics: a failed attempt still incurs cost that should be
    accounted for in the cost-per-merged calculation. This test creates a chain
    where a fix task fails, then a re-review and successful fix follow, so the
    FAILED task's cost is included in the final chain cost.
    """
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-with-failed-fix",
        goal="lane-with-failed-fix",
        branch_name="lane-with-failed-fix",
        base_branch="main",
        pilot_arm="lane",
    )
    lane.reserved_idle_cost_usd_total = 0.0

    root_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
        },
        estimated_cost_usd=0.5,
    )
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "review",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )
    # First fix attempt that FAILS
    failed_fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=0.8,
    )
    # Re-review after the failed fix
    re_review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "re-review",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )
    # Successful fix after re-review
    successful_fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": True,
        },
        estimated_cost_usd=1.2,
    )

    db_session.add_all(
        [
            lane,
            root_task,
            review_task,
            failed_fix_task,
            re_review_task,
            successful_fix_task,
        ]
    )
    await db_session.commit()
    await db_session.refresh(lane)

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    # Verify the chain is counted as one lane chain
    assert agg["n_lane"] == 1
    assert agg["n_control"] == 0

    # Total cost should include the failed fix task's cost:
    # root(0.5) + review(1.0) + failed_fix(0.8) + re_review(1.0) + successful_fix(1.2) = 4.5
    assert agg["lane_runtime_cost_per_merged"] == pytest.approx(4.5)
    assert agg["lane_cost_per_merged"] == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_include_cancelled_fix_task_cost(db_session):
    """Verify a CANCELLED fix task's cost is included in lane_cost_per_merged.

    Intent-to-treat semantics: a cancelled attempt still incurs cost that should be
    accounted for in the cost-per-merged calculation. This test creates a chain where
    a fix task is cancelled, then a re-review and successful fix follow, so the
    CANCELLED task's cost is included in the final chain cost. The cancelled task's
    pr_merged=False must not inflate the merged count.
    """
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-with-cancelled-fix",
        goal="lane-with-cancelled-fix",
        branch_name="lane-with-cancelled-fix",
        base_branch="main",
        pilot_arm="lane",
    )
    lane.reserved_idle_cost_usd_total = 0.0

    root_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
        },
        estimated_cost_usd=0.5,
    )
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "review",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )
    # First fix attempt that is CANCELLED — cost still counts under ITT semantics
    cancelled_fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=0.8,
    )
    # Re-review after the cancelled fix
    re_review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "re-review",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": False,
        },
        estimated_cost_usd=1.0,
    )
    # Successful fix after re-review
    successful_fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root_task.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "pr_merged": True,
        },
        estimated_cost_usd=1.2,
    )

    db_session.add_all(
        [
            lane,
            root_task,
            review_task,
            cancelled_fix_task,
            re_review_task,
            successful_fix_task,
        ]
    )
    await db_session.commit()
    await db_session.refresh(lane)

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    # Verify the chain is counted as one lane chain (cancelled task must not inflate merged count)
    assert agg["n_lane"] == 1
    assert agg["n_control"] == 0

    # Total cost should include the cancelled fix task's cost:
    # root(0.5) + review(1.0) + cancelled_fix(0.8) + re_review(1.0) + successful_fix(1.2) = 4.5
    assert agg["lane_runtime_cost_per_merged"] == pytest.approx(4.5)
    assert agg["lane_cost_per_merged"] == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_shadow_pilot_arm_counted_in_n_shadow_not_n_lane(db_session):
    """Tasks with pilot_arm='shadow' are counted under n_shadow, not n_lane/n_control."""
    project = await _make_project(db_session, is_self_repo=True)

    shadow_pilot_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "shadow", "base_branch": "main"}],
        context={LANE_PILOT_ASSIGNMENT_KEY: _SHADOW_ARM},
        estimated_cost_usd=1.0,
    )
    shadow_pilot_task2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "review",
                "prompt": "review",
                "base_branch": "main",
                "original_task_id": str(shadow_pilot_task.id),
            }
        ],
        context={LANE_PILOT_ASSIGNMENT_KEY: _SHADOW_ARM},
        estimated_cost_usd=0.5,
    )
    db_session.add_all([shadow_pilot_task, shadow_pilot_task2])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)

    # Shadow arms are counted in n_shadow, NOT in n_lane or n_control.
    assert agg["n_shadow"] == 1, "one shadow chain (two tasks, same original_task_id)"
    assert agg["n_lane"] == 0
    assert agg["n_control"] == 0


@pytest.mark.asyncio
async def test_record_lane_arm_assignment_fires_in_shadow_pilot_mode(mocker):
    """record_lane_arm_assignment writes an arm assignment in shadow pilot mode."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": False,
            "review_fix_lane_pilot_mode": "shadow",
        },
        clear=False,
    )
    project = make_project(is_self_repo=True)
    ctx: dict = {}
    arm = record_lane_arm_assignment(ctx, "seed-task-id-123", project)
    assert arm in ("lane", "control"), f"expected lane/control arm, got {arm!r}"
    assert ctx.get(LANE_PILOT_ASSIGNMENT_KEY) == arm


@pytest.mark.asyncio
async def test_record_lane_arm_assignment_silent_when_mode_off(mocker):
    """record_lane_arm_assignment is a no-op when mode is off and pilot is disabled."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": False,
            "review_fix_lane_pilot_mode": "off",
        },
        clear=False,
    )
    project = make_project(is_self_repo=True)
    ctx: dict = {}
    arm = record_lane_arm_assignment(ctx, "seed-task-id-999", project)
    assert arm is None
    assert LANE_PILOT_ASSIGNMENT_KEY not in ctx


@pytest.mark.asyncio
async def test_shadow_pilot_mode_active_for_eligible_project(mocker):
    """is_review_fix_lane_pilot_shadow_mode_active_for_project returns True for eligible project."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": False,
            "review_fix_lane_pilot_mode": "shadow",
        },
        clear=False,
    )
    project = make_project(is_self_repo=True)
    assert is_review_fix_lane_pilot_shadow_mode_active_for_project(project) is True


@pytest.mark.asyncio
async def test_shadow_pilot_mode_inactive_for_ineligible_project(mocker):
    """is_review_fix_lane_pilot_shadow_mode_active_for_project returns False for non-self-repo."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": False,
            "review_fix_lane_pilot_mode": "shadow",
        },
        clear=False,
    )
    project = make_project(is_self_repo=False)
    assert is_review_fix_lane_pilot_shadow_mode_active_for_project(project) is False


@pytest.mark.asyncio
async def test_checkpoint_skips_warm_reservation_for_shadow_arm_lane(
    db_session, mocker
):
    """checkpoint_and_reserve_on_phase_complete never takes a warm reservation for
    a lane with pilot_arm='shadow' — the lane is observational only."""
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.improvement_lane_ops.checkpoint_lane", new_callable=AsyncMock)
    mock_release = mocker.patch("app.warm_pool.release_pool_vm", new_callable=AsyncMock)

    project = await _make_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="shadow-pilot-lane",
        goal="observe",
        branch_name="feat/shadow-pilot",
        base_branch="main",
        pilot_arm=_SHADOW_ARM,
    )
    await db_session.flush()
    await cas_transition(
        db_session,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=None,
    )
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        vm_instance_id="pool-vm-shadow-99",
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    await db_session.refresh(lane)

    result = await checkpoint_and_reserve_on_phase_complete(
        db_session,
        source_task,
        lane,
        sha="abc1234def5678",
        summary="shadow phase done",
        reserve_workspace=True,  # caller says True, but shadow arm overrides
    )

    assert result is True, "checkpoint should be written for shadow arm lanes"
    mock_release.assert_not_called()


def test_lane_verdict_cost_includes_reserved_idle_cost():
    # With reserved idle, lane total cost = 4.6 vs control 4.0 → +15% > KILL ceiling (10%)
    agg_with_idle = {
        "n_lane": 50,
        "n_control": 50,
        "lane_merge_rate": 0.80,
        "control_merge_rate": 0.80,
        "lane_cost_per_merged": 4.6,
        "control_cost_per_merged": 4.0,
        "lane_runtime_cost_per_merged": 3.0,
        "control_runtime_cost_per_merged": 4.0,
        "lane_reserved_idle_cost_per_merged": 1.6,
        "control_reserved_idle_cost_per_merged": 0.0,
        "gate_integrity_violations": 0,
        "checkpoint_loss_rate": None,
        "warm_resume_fraction": 0.9,
        "checkpoint_fraction": 0.9,
        "lane_fix_tasks_sampled": 50,
    }
    verdict_with_idle = evaluate_lane_pilot_verdict(agg_with_idle)
    assert verdict_with_idle["verdict"] == "KILL"
    assert verdict_with_idle["verdict_reason"] == "lane_cost_ceiling_exceeded"
    assert "cost_delta_pct" in verdict_with_idle
    assert verdict_with_idle["cost_delta_pct"] == pytest.approx(15.0, abs=0.1)

    # Without reserved idle, runtime-only cost = 3.0 vs control 4.0 → -25% (no cost kill)
    agg_runtime_only = {
        **agg_with_idle,
        "lane_cost_per_merged": 3.0,
        "lane_reserved_idle_cost_per_merged": 0.0,
    }
    verdict_runtime_only = evaluate_lane_pilot_verdict(agg_runtime_only)
    assert verdict_runtime_only["verdict"] != "KILL"
