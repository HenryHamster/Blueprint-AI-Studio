"""Focused tests for review-fix lane-pilot cost attribution."""

import uuid

import pytest
from datetime import timedelta
from sqlalchemy import select
from unittest.mock import AsyncMock

from app.improvement_lane_ops import create_lane
from app.models import ImprovementLane
from app.review_fix_lane_pilot import (
    LANE_CHAIN_ID_KEY,
    _SHADOW_ARM,
    compute_lane_arm_time_medians,
    compute_lane_pilot_aggregates,
    record_lane_arm_assignment,
)
from app.capability_trial_verdict import (
    LANE_PILOT_ASSIGNMENT_KEY,
    LANE_PILOT_CHECKPOINT_RECEIPT_KEY,
    LANE_PILOT_WARM_RESUME_RECEIPT_KEY,
    evaluate_lane_pilot_verdict,
)
from app.models import TaskStatus
from app.time_utils import _utcnow
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
async def test_record_lane_arm_assignment_silent_when_pilot_disabled(mocker):
    """record_lane_arm_assignment is a no-op when pilot is disabled (no shadow mode)."""
    mocker.patch.dict(
        "app.shared_state.runtime_config",
        {
            "improvement_lanes_enabled": True,
            "review_fix_lane_pilot_enabled": False,
        },
        clear=False,
    )
    project = make_project(is_self_repo=True)
    ctx: dict = {}
    arm = record_lane_arm_assignment(ctx, "seed-task-id-999", project)
    assert arm is None
    assert LANE_PILOT_ASSIGNMENT_KEY not in ctx


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
    lane.current_task_id = source_task.id
    db_session.add(lane)
    await db_session.commit()
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


@pytest.mark.asyncio
async def test_gate_integrity_violations_counts_review_with_lane_id(db_session):
    """gate_integrity_violations is 1 when a review task carries improvement_lane_id."""
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="gate-test-lane",
        goal="gate-test-lane",
        branch_name="gate-test-lane",
        base_branch="main",
        pilot_arm="lane",
    )

    # A review task that incorrectly carries improvement_lane_id in its context.
    bad_review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "prompt": "review", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane.id),
            "improvement_lane_id": str(lane.id),
        },
        estimated_cost_usd=0.5,
    )

    db_session.add_all([lane, bad_review_task])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["gate_integrity_violations"] == 1

    # A separate project with no such task should report 0 violations.
    clean_project = await _make_project(db_session, is_self_repo=True)
    agg_clean = await compute_lane_pilot_aggregates(db_session, clean_project.id)
    assert agg_clean["gate_integrity_violations"] == 0


@pytest.mark.asyncio
async def test_checkpoint_loss_rate_reflects_phase_requeues(db_session):
    """checkpoint_loss_rate is the attempt-level fraction of checkpoint phases lost."""
    project = await _make_project(db_session, is_self_repo=True)

    # Lane 1: 4 successful checkpoints, no requeues
    lane_ok = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-no-requeue",
        goal="lane-no-requeue",
        branch_name="lane-no-requeue",
        base_branch="main",
        pilot_arm="lane",
    )
    lane_ok.phase_requeues = 0
    lane_ok.checkpoint_seq = 4

    # Lane 2: 5 successful checkpoints, 1 requeue (checkpoint loss)
    lane_lost = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-requeued",
        goal="lane-requeued",
        branch_name="lane-requeued",
        base_branch="main",
        pilot_arm="lane",
    )
    lane_lost.phase_requeues = 1
    lane_lost.checkpoint_seq = 5

    db_session.add_all([lane_ok, lane_lost])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    # total_requeues=1, total_attempts=10 → rate = 0.1
    assert agg["checkpoint_loss_rate"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_checkpoint_loss_rate_attempt_level_formula(db_session):
    """Attempt-level rate stays below 5% kill ceiling even when a lane had requeues.

    Two lanes each with checkpoint_seq=10 and one with phase_requeues=1:
    rate = 1 / (20 + 1) ≈ 0.0476 — below kill ceiling — whereas the old
    lane-level formula (1/2 = 0.5) would have triggered a premature KILL.
    """
    project = await _make_project(db_session, is_self_repo=True)
    for i in range(2):
        lane = await create_lane(
            db_session,
            project_id=project.id,
            direction_id=None,
            title=f"lane-attempt-{i}",
            goal=f"lane-attempt-{i}",
            branch_name=f"branch-attempt-{i}",
            base_branch="main",
            pilot_arm="lane",
        )
        lane.checkpoint_seq = 10
        lane.phase_requeues = 1 if i == 0 else 0
        db_session.add(lane)
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    # 1 requeue / 21 attempts ≈ 0.0476 (below the 0.05 kill ceiling)
    assert agg["checkpoint_loss_rate"] == pytest.approx(1 / 21)
    assert agg["checkpoint_loss_rate"] < 0.05


@pytest.mark.asyncio
async def test_checkpoint_loss_rate_diagnostic_fields_present(db_session):
    """checkpoint_attempts_total and checkpoint_requeues_total are returned."""
    project = await _make_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="lane-diag",
        goal="lane-diag",
        branch_name="branch-diag",
        base_branch="main",
        pilot_arm="lane",
    )
    lane.checkpoint_seq = 8
    lane.phase_requeues = 2
    db_session.add(lane)
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["checkpoint_attempts_total"] == 10  # 8 + 2
    assert agg["checkpoint_requeues_total"] == 2


@pytest.mark.asyncio
async def test_warm_resume_fraction_from_fix_task_context(db_session):
    """warm_resume_fraction is 1.0 when the fix task carries the warm-resume receipt."""
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="warm-resume-lane",
        goal="warm-resume-lane",
        branch_name="warm-resume-lane",
        base_branch="main",
        pilot_arm="lane",
    )

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
            LANE_PILOT_WARM_RESUME_RECEIPT_KEY: True,
        },
        estimated_cost_usd=1.0,
    )

    db_session.add_all([lane, root_task, fix_task])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["warm_resume_fraction"] == pytest.approx(1.0)

    # Without the receipt, fraction should be None (no fix tasks).
    empty_project = await _make_project(db_session, is_self_repo=True)
    agg_empty = await compute_lane_pilot_aggregates(db_session, empty_project.id)
    assert agg_empty["warm_resume_fraction"] is None


@pytest.mark.asyncio
async def test_checkpoint_fraction_from_fix_task_context(db_session):
    """checkpoint_fraction is 1.0 when the fix task carries the checkpoint receipt."""
    project = await _make_project(db_session, is_self_repo=True)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="checkpoint-lane",
        goal="checkpoint-lane",
        branch_name="checkpoint-lane",
        base_branch="main",
        pilot_arm="lane",
    )

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
            LANE_PILOT_CHECKPOINT_RECEIPT_KEY: True,
        },
        estimated_cost_usd=1.0,
    )

    db_session.add_all([lane, root_task, fix_task])
    await db_session.commit()

    agg = await compute_lane_pilot_aggregates(db_session, project.id)
    assert agg["checkpoint_fraction"] == pytest.approx(1.0)

    # A fix task without the receipt should yield 0.0, not None.
    project2 = await _make_project(db_session, is_self_repo=True)
    lane2 = await create_lane(
        db_session,
        project_id=project2.id,
        direction_id=None,
        title="no-ckpt-lane",
        goal="no-ckpt-lane",
        branch_name="no-ckpt-lane",
        base_branch="main",
        pilot_arm="lane",
    )
    root2 = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "create", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane2.id),
        },
        estimated_cost_usd=0.3,
    )
    fix2 = make_task(
        project_id=project2.id,
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix",
                "base_branch": "main",
                "original_task_id": str(root2.id),
            }
        ],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            LANE_CHAIN_ID_KEY: str(lane2.id),
            "pr_merged": True,
            # No checkpoint receipt key
        },
        estimated_cost_usd=0.8,
    )
    db_session.add_all([lane2, root2, fix2])
    await db_session.commit()

    agg2 = await compute_lane_pilot_aggregates(db_session, project2.id)
    assert agg2["checkpoint_fraction"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_lane_relay_comparison_splits_fix_phase(db_session):
    """compute_lane_arm_time_medians reports fix_active_seconds and review_active_seconds
    independently, so review-side noise cannot mask fix-side improvements."""
    project = await _make_project(db_session, is_self_repo=True)
    now = _utcnow()
    pr_number = 42

    # Review task: active for 100s (started_at to updated_at)
    review_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "prompt": "review", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"pr_number": pr_number},
        },
        created_at=now - timedelta(seconds=400),
        started_at=now - timedelta(seconds=300),
        updated_at=now - timedelta(seconds=200),
    )

    # Fix task: active for 200s
    fix_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={
            LANE_PILOT_ASSIGNMENT_KEY: "lane",
            "review_loop": {"pr_number": pr_number},
        },
        created_at=now - timedelta(seconds=400),
        started_at=now - timedelta(seconds=195),
        updated_at=now + timedelta(seconds=5),
    )

    db_session.add_all([review_task, fix_task])
    await db_session.commit()

    medians = await compute_lane_arm_time_medians(db_session, project.id)

    assert medians["fix_active_seconds"] == pytest.approx(200.0)
    assert medians["review_active_seconds"] == pytest.approx(100.0)
    # Aggregate active equals the sum of phase actives for a single PR
    assert medians["active_seconds"] == pytest.approx(300.0)
    assert (
        medians["fix_active_seconds"] + medians["review_active_seconds"]
    ) == pytest.approx(medians["active_seconds"])


# ---------------------------------------------------------------------------
# compute_lane_baseline_trend
# ---------------------------------------------------------------------------


def _make_baseline(
    version_number: int,
    *,
    wall_clock: float,
    active: float,
    gap: float,
    task_count: float,
    fix_p50: float,
    fix_p90: float,
    review_p50: float,
    review_p90: float,
    waste_taxonomy: dict,
    waste_task_count: int,
    terminal_status: str = "promoted",
) -> dict:
    return {
        "version_number": version_number,
        "terminal_status": terminal_status,
        "relay_medians": {
            "wall_clock_seconds": wall_clock,
            "active_seconds": active,
            "gap_seconds": gap,
            "task_count": task_count,
            "pr_count": 5,
        },
        "lane_iteration_distribution": {
            "fix": {
                "p50_iterations": fix_p50,
                "p90_iterations": fix_p90,
                "max_iterations": fix_p90,
                "task_count": 10,
                "pr_count": 5,
            },
            "review": {
                "p50_iterations": review_p50,
                "p90_iterations": review_p90,
                "max_iterations": review_p90,
                "task_count": 10,
                "pr_count": 5,
            },
        },
        "lane_waste": {
            "waste_taxonomy": waste_taxonomy,
            "waste_task_count": waste_task_count,
        },
    }


def test_lane_baseline_cross_version_trend():
    """compute_lane_baseline_trend returns correct deltas and classifications."""
    from app.digest_metrics import compute_lane_baseline_trend

    # Baselines are newest-first (latest = index 0).
    latest = _make_baseline(
        version_number=5,
        wall_clock=1800.0,  # improved by 20% vs prior 2250
        active=900.0,  # unchanged vs prior 900
        gap=900.0,  # worsened vs prior 700 (~28%)
        task_count=2.0,  # improved vs prior 3.0 (delta -1.0 > 0.5 abs)
        fix_p50=2.0,  # improved vs prior 3.0 (delta -1.0 > 0.5 abs)
        fix_p90=4.0,  # stable vs prior 4.2 (delta -0.2 < 0.5 abs)
        review_p50=1.0,  # stable vs prior 1.0
        review_p90=2.0,  # stable vs prior 2.0
        waste_taxonomy={
            "infra": 0,
            "merge_conflict": 2,
            "ci_churn": 0,
            "review_ambiguity": 0,
            "lost_reasoning": 5,
            "lost_repo_state": 0,
            "unknown": 3,
        },
        waste_task_count=10,
    )
    prior = _make_baseline(
        version_number=4,
        wall_clock=2250.0,
        active=900.0,
        gap=700.0,
        task_count=3.0,
        fix_p50=3.0,
        fix_p90=4.2,
        review_p50=1.0,
        review_p90=2.0,
        waste_taxonomy={
            "infra": 0,
            "merge_conflict": 1,
            "ci_churn": 0,
            "review_ambiguity": 0,
            "lost_reasoning": 2,
            "lost_repo_state": 0,
            "unknown": 2,
        },
        waste_task_count=5,
    )

    trend = compute_lane_baseline_trend([latest, prior])
    assert trend is not None
    assert trend["latest_version"] == 5
    assert trend["prior_version"] == 4

    # wall_clock improved by 20% — threshold is ±10%
    wc = trend["relay_medians"]["wall_clock_seconds"]
    assert wc["classification"] == "improving"
    assert wc["delta"] == pytest.approx(1800.0 - 2250.0)

    # active stable (delta = 0)
    active = trend["relay_medians"]["active_seconds"]
    assert active["classification"] == "stable"
    assert active["delta"] == pytest.approx(0.0)

    # gap worsened by ~28%
    gap = trend["relay_medians"]["gap_seconds"]
    assert gap["classification"] == "declining"
    assert gap["delta"] == pytest.approx(200.0)

    # task_count improved: delta = -1.0, threshold abs 0.5
    tc = trend["relay_medians"]["task_count"]
    assert tc["classification"] == "improving"
    assert tc["delta"] == pytest.approx(-1.0)

    # fix p50 improved: delta = -1.0
    fix_p50 = trend["lane_iterations"]["fix"]["p50"]
    assert fix_p50["classification"] == "improving"

    # fix p90 stable: delta = -0.2 < 0.5
    fix_p90 = trend["lane_iterations"]["fix"]["p90"]
    assert fix_p90["classification"] == "stable"

    # review p50/p90 stable
    assert trend["lane_iterations"]["review"]["p50"]["classification"] == "stable"
    assert trend["lane_iterations"]["review"]["p90"]["classification"] == "stable"

    # lost_reasoning: latest share = 5/10 = 0.5; prior = 2/5 = 0.4; delta = +0.1 > 0.05 → declining
    lr = trend["waste_taxonomy_shares"]["lost_reasoning"]
    assert lr["classification"] == "declining"
    assert lr["latest"] == pytest.approx(0.5)
    assert lr["prior"] == pytest.approx(0.4)

    # merge_conflict: latest = 2/10 = 0.2; prior = 1/5 = 0.2; delta = 0 → stable
    mc = trend["waste_taxonomy_shares"]["merge_conflict"]
    assert mc["classification"] == "stable"


@pytest.mark.asyncio
async def test_checkpoint_cost_scalars_propagated_to_payload(db_session, mocker):
    """checkpoint_and_reserve_on_phase_complete propagates LLM + VM cost scalars.

    A bug zeroing or omitting tokens_in, tokens_out, or vm_cost_usd on the
    LaneCheckpointPayload must cause this test to fail.
    """
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

    TOKENS_IN = 1234
    TOKENS_OUT = 5678
    VM_COST = 0.42

    mocker.patch(
        "app.models.get_task_total_llm_totals",
        return_value=(TOKENS_IN, TOKENS_OUT, 0),
    )
    mocker.patch(
        "app.models.get_task_total_vm_cost_usd",
        return_value=VM_COST,
    )

    captured_payloads: list = []

    async def _capture_checkpoint(session, lane_id, *, phase, task_id, checkpoint):
        captured_payloads.append(checkpoint)

    mocker.patch(
        "app.improvement_lane_ops.checkpoint_lane",
        side_effect=_capture_checkpoint,
    )

    project = await _make_project(db_session, is_self_repo=True)
    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="cost-scalar-lane",
        goal="cost-scalar",
        branch_name="feat/cost-scalar",
        base_branch="main",
        pilot_arm="lane",
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
        vm_instance_id=None,
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    lane.current_task_id = source_task.id
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    result = await checkpoint_and_reserve_on_phase_complete(
        db_session,
        source_task,
        lane,
        sha="abc1234def5678",
        summary="cost scalar test",
        reserve_workspace=False,
    )

    assert result is True, "checkpoint should be written"
    assert len(captured_payloads) == 1, "checkpoint_lane must be called exactly once"
    payload = captured_payloads[0]
    assert payload.tokens_in == TOKENS_IN, (
        f"tokens_in mismatch: got {payload.tokens_in}, expected {TOKENS_IN}"
    )
    assert payload.tokens_out == TOKENS_OUT, (
        f"tokens_out mismatch: got {payload.tokens_out}, expected {TOKENS_OUT}"
    )
    assert payload.vm_cost_usd == pytest.approx(VM_COST), (
        f"vm_cost_usd mismatch: got {payload.vm_cost_usd}, expected {VM_COST}"
    )


def test_lane_baseline_trend_returns_none_when_fewer_than_two():
    """compute_lane_baseline_trend returns None for 0 or 1 baselines."""
    from app.digest_metrics import compute_lane_baseline_trend

    assert compute_lane_baseline_trend([]) is None
    single = _make_baseline(
        version_number=1,
        wall_clock=1000.0,
        active=500.0,
        gap=500.0,
        task_count=2.0,
        fix_p50=2.0,
        fix_p90=4.0,
        review_p50=1.0,
        review_p90=2.0,
        waste_taxonomy={
            "infra": 0,
            "merge_conflict": 0,
            "ci_churn": 0,
            "review_ambiguity": 0,
            "lost_reasoning": 0,
            "lost_repo_state": 0,
            "unknown": 0,
        },
        waste_task_count=0,
    )
    assert compute_lane_baseline_trend([single]) is None


@pytest.mark.asyncio
async def test_emit_lane_event_message_dedupe_is_db_bounded(db_session):
    """emit_lane_event_message uses a DB-side dedupe filter (LIMIT 1), not a full scan.

    Verifies:
    1. A message is inserted on first call.
    2. A duplicate is NOT inserted on a second call with the same dedupe_key.
    3. A different dedupe_key does get its own message.
    """
    from sqlalchemy import select as sa_select

    from app.models import MessageType, ProjectMessage
    from app.review_fix_lane_pilot import emit_lane_event_message
    from testing.factories import make_project

    project = make_project(is_self_repo=True)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lane_id = uuid.uuid4()

    # First call: should insert one message.
    await emit_lane_event_message(
        db_session, project.id, lane_id, "test_event", "detail A"
    )
    await db_session.commit()

    msgs = (
        (
            await db_session.execute(
                sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    assert msgs[0].message_type == MessageType.NOTE
    assert msgs[0].context["dedupe_key"] == f"lane_test_event_{lane_id}"

    # Second call with same args: dedupe should prevent a second insert.
    await emit_lane_event_message(
        db_session, project.id, lane_id, "test_event", "detail A again"
    )
    await db_session.commit()

    msgs2 = (
        (
            await db_session.execute(
                sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs2) == 1, "duplicate insert must be suppressed by DB-side dedupe"

    # Different event type: a new message should be inserted.
    await emit_lane_event_message(
        db_session, project.id, lane_id, "other_event", "detail B"
    )
    await db_session.commit()

    msgs3 = (
        (
            await db_session.execute(
                sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs3) == 2, "distinct dedupe_key must produce a separate message"


@pytest.mark.asyncio
async def test_raise_gate_integrity_alert_dedupe_is_db_bounded(db_session):
    """raise_gate_integrity_alert uses a DB-side dedupe filter (LIMIT 1), not a full scan.

    Verifies:
    1. A BLOCKER message is inserted on first call.
    2. A duplicate is NOT inserted on a second call.
    """
    from sqlalchemy import select as sa_select

    from app.models import MessageType, ProjectMessage
    from app.review_fix_lane_pilot import raise_gate_integrity_alert
    from testing.factories import make_project

    project = make_project(is_self_repo=True)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First call: should insert one BLOCKER message.
    await raise_gate_integrity_alert(db_session, project.id, "gate integrity broken")
    await db_session.commit()

    msgs = (
        (
            await db_session.execute(
                sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    assert msgs[0].message_type == MessageType.BLOCKER
    assert msgs[0].context["dedupe_key"] == "review_fix_lane_gate_integrity_violation"

    # Second call: dedupe must suppress the second insert.
    await raise_gate_integrity_alert(
        db_session, project.id, "gate integrity broken again"
    )
    await db_session.commit()

    msgs2 = (
        (
            await db_session.execute(
                sa_select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs2) == 1, "duplicate BLOCKER must be suppressed by DB-side dedupe"
