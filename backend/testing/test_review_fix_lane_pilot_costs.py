"""Focused tests for review-fix lane-pilot cost attribution."""

import pytest
from sqlalchemy import select

from app.improvement_lane_ops import create_lane
from app.models import ImprovementLane
from app.review_fix_lane_pilot import LANE_CHAIN_ID_KEY, compute_lane_pilot_aggregates
from app.capability_trial_verdict import LANE_PILOT_ASSIGNMENT_KEY
from app.models import TaskStatus
from testing.factories import make_project, make_task


async def _make_project(db_session, **project_kwargs):
    project = make_project(**project_kwargs)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_lane_pilot_aggregates_include_reserved_idle_and_exclude_shadow(db_session):
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

    db_session.add_all([lane, shadow_lane, root_task, review_task, fix_task, control_task])
    await db_session.commit()
    await db_session.refresh(lane)
    await db_session.refresh(shadow_lane)

    lanes = (
        await db_session.execute(
            select(ImprovementLane).where(ImprovementLane.project_id == project.id)
        )
    ).scalars().all()
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
