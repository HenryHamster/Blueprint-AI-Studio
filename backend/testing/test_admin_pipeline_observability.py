"""Endpoint regressions for outer-loop convergence observability."""

from datetime import timedelta

import pytest

from app.models import TaskStatus
from testing.factories import (
    make_alert,
    make_membership,
    make_project,
    make_task,
    _utcnow,
)


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


@pytest.mark.asyncio
async def test_outer_loop_summary_includes_dominant_blocker(client, db_session):
    """The convergence summary endpoint surfaces the additive dominant_blocker
    key verbatim (the endpoint spreads the summary dict). The dominant blocker is
    the single candidate CAUSE present in the most non-converged PR-chains, gated
    so it must cover >=2 chains and >=50% of them; here both non-merged chains
    churned, so ``churn`` dominates with share 1.0 over a non_converged_total of 2."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    tasks = [
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            context={
                "pr_rejected": True,
                "review_loop": {"iteration": 2, "pr_number": 1, "churn_detections": 1},
            },
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            context={
                "pr_rejected": True,
                "review_loop": {"iteration": 3, "pr_number": 2, "churn_detections": 2},
            },
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"pr_merged": True, "review_loop": {"iteration": 1, "pr_number": 3}},
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    resp = await client.get(f"/api/admin/outer-loop-convergence?hours=24&project_id={project.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["non_converged_total"] == 2
    assert data["dominant_blocker"] is not None
    assert data["dominant_blocker"]["cause"] == "churn"
    assert data["dominant_blocker"]["count"] == 2
    assert data["dominant_blocker"]["total"] == 2
    assert data["dominant_blocker"]["share"] == 1.0


@pytest.mark.asyncio
async def test_outer_loop_chains_drilldown_lists_nonmerged(client, db_session):
    """The drill-down lists the actual non-merged PR-chains with enough context
    to investigate; merged chains are excluded when no reason filter is given."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    rejected = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=1),
        context={
            "pr_rejected": True,
            "review_loop": {"iteration": 3, "pr_number": 42, "churn_detections": 2},
        },
    )
    merged = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=2),
        context={"pr_merged": True, "review_loop": {"iteration": 1, "pr_number": 43}},
    )
    db_session.add_all([rejected, merged])
    await db_session.commit()

    resp = await client.get(f"/api/admin/outer-loop-chains?hours=24&project_id={project.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_hours"] == 24
    assert data["reason_filter"] is None
    # Only the non-merged (rejected) chain is listed by default.
    assert data["total"] == 1
    row = data["chains"][0]
    assert row["pr_number"] == 42
    assert row["terminal_reason"] == "rejected"
    assert row["max_iteration"] == 3
    assert row["churn_count"] == 2
    assert str(rejected.id) in row["task_ids"]
    assert row["project_id"] == str(project.id)


@pytest.mark.asyncio
async def test_outer_loop_chains_reason_filter(client, db_session):
    """A reason filter narrows results to chains with that terminal outcome; an
    unknown reason is rejected with 400."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    rejected = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=1),
        context={"pr_rejected": True, "review_loop": {"iteration": 2, "pr_number": 42}},
    )
    cap = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=1),
        context={
            "failure_detail": "review_cap_exhausted",
            "review_loop": {"iteration": 16, "pr_number": 50},
        },
    )
    db_session.add_all([rejected, cap])
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/outer-loop-chains?hours=24&project_id={project.id}&reason=cap_exhausted"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reason_filter"] == "cap_exhausted"
    assert data["total"] == 1
    assert data["chains"][0]["pr_number"] == 50
    assert data["chains"][0]["terminal_reason"] == "cap_exhausted"

    bad = await client.get(
        f"/api/admin/outer-loop-chains?hours=24&project_id={project.id}&reason=bogus"
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_warm_reuse_dropout_names_dominant_leader(client, db_session):
    """The drop-out endpoint surfaces the ranked breakdown and names the single
    dominant reason when one clearly dominates the non-converting population."""
    import app.warm_backlog_reuse as wbr

    wbr._claim_skipped_events.clear()
    wbr._grace_idle_release_events.clear()
    wbr._grace_ceiling_release_events.clear()
    wbr._scan_skipped_depends_on_events.clear()
    wbr._scan_skipped_pinned_vm_events.clear()
    wbr._scan_skipped_file_conflict_events.clear()
    wbr._scan_saturated_events.clear()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    for _ in range(4):
        wbr.record_claim_skipped("lost_claim_race")
    wbr.record_grace_ceiling_release()

    resp = await client.get(f"/api/admin/projects/{project.id}/warm-reuse-dropout")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dropout_total"] == 5
    assert data["dropout_by_reason"]["claim_lost_claim_race"] == 4
    assert data["dominant_dropout"]["reason"] == "claim_lost_claim_race"
    assert data["dominant_dropout"]["share"] == 0.8
    assert "per-VM" in data["attribution_note"]


@pytest.mark.asyncio
async def test_warm_reuse_dropout_trend_reads_persisted_snapshots(client, db_session):
    """The trend endpoint reads the ``reuse_dropout`` block persisted into each
    daily efficiency snapshot, and ignores snapshots that lack the block."""
    from app.efficiency_snapshot import DESIGN_DOC_KEY

    project = make_project()
    project.design_doc = {
        DESIGN_DOC_KEY: {
            "snapshots": [
                {"captured_at": "2026-05-30T00:00:00"},  # no reuse_dropout -> skipped
                {
                    "captured_at": "2026-05-31T00:00:00",
                    "reuse_dropout": {
                        "dropout_total": 5,
                        "dominant_dropout": {"reason": "claim_lost_claim_race", "share": 0.8},
                    },
                },
            ]
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/projects/{project.id}/warm-reuse-dropout-trend"
    )
    assert resp.status_code == 200
    buckets = resp.json()["buckets"]
    assert len(buckets) == 1
    assert buckets[0]["captured_at"] == "2026-05-31T00:00:00"
    assert buckets[0]["dropout_total"] == 5
    assert buckets[0]["dominant_reason"] == "claim_lost_claim_race"
    assert buckets[0]["dominant_share"] == 0.8
