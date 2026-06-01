"""Endpoint regressions for outer-loop convergence observability."""

from datetime import timedelta

import pytest

from app.models import TaskStatus
from testing.factories import make_alert, make_project, make_task, _utcnow


@pytest.mark.asyncio
async def test_outer_loop_convergence_hydrates_original_task_outside_window(
    client,
    db_session,
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    original = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=72),
        context={"pr_merged": True},
    )
    child = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        context={
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original.id),
                "pr_number": 42,
            }
        },
    )
    db_session.add_all([original, child])
    await db_session.commit()

    resp = await client.get("/api/admin/outer-loop-convergence?hours=24")
    assert resp.status_code == 200
    data = resp.json()
    assert data["prs_with_review_loop"] == 1
    assert data["outcomes"]["merged"] == 1
    assert data["convergence_rate"] == 1.0


@pytest.mark.asyncio
async def test_outer_loop_convergence_trend_buckets_each_cycle_on_its_own_day(
    client,
    db_session,
):
    """Trend buckets attribute each review/fix cycle to its own creation day.

    The endpoint buckets tasks by their own ``created_at`` day and alerts by
    theirs (per-day-per-cycle attribution), folding the chain-original into
    whichever day's bucket references it so ``pr_merged`` is still attributed.
    A two-cycle chain whose iterations land on different days therefore shows
    one converged PR on each day, with the escalation alert counted only on the
    day it was raised.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    day_1 = now - timedelta(days=1, hours=1)
    day_2 = now - timedelta(hours=1)

    original = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(days=3),
        context={"pr_merged": True},
    )
    first_cycle = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=day_1,
        context={
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original.id),
                "pr_number": 42,
            }
        },
    )
    second_cycle = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=day_2,
        context={
            "review_loop": {
                "iteration": 2,
                "original_task_id": str(original.id),
                "pr_number": 42,
            }
        },
    )
    db_session.add_all([original, first_cycle, second_cycle])
    await db_session.commit()

    alert = make_alert(
        project.id,
        task_id=second_cycle.id,
        title="Review-fix escalation: stuck cycle on PR #42",
        created_at=day_2,
    )
    db_session.add(alert)
    await db_session.commit()

    resp = await client.get("/api/admin/outer-loop-convergence-trend?days=2")
    assert resp.status_code == 200
    data = resp.json()
    buckets = {bucket["date"]: bucket for bucket in data["buckets"]}
    # Per-day-per-cycle attribution: day_1 holds iteration 1 (converged via the
    # folded original's pr_merged) with no alert; day_2 holds iteration 2 (also
    # converged) plus the escalation alert raised that day.
    assert buckets[day_1.strftime("%Y-%m-%d")]["pr_count"] == 1
    assert buckets[day_1.strftime("%Y-%m-%d")]["convergence_rate"] == 1.0
    assert buckets[day_1.strftime("%Y-%m-%d")]["escalation_total"] == 0
    assert buckets[day_2.strftime("%Y-%m-%d")]["pr_count"] == 1
    assert buckets[day_2.strftime("%Y-%m-%d")]["convergence_rate"] == 1.0
    assert buckets[day_2.strftime("%Y-%m-%d")]["escalation_total"] == 1
