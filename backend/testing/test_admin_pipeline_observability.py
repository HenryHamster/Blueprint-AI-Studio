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
async def test_pipeline_regressions_includes_viewport_metadata_from_failed_task_key(
    client,
    db_session,
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    failed = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=_utcnow() - timedelta(hours=1),
        latest_status_message="viewport assertion failed",
        context={
            "failure_category": "VIEWPORT_REGRESSION",
            "viewport_regression:TaskCard:overflow@375": True,
        },
    )
    db_session.add(failed)
    await db_session.commit()

    resp = await client.get(f"/api/admin/pipeline-regressions?project_id={project.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == str(project.id)
    assert data["healthy"] is False
    row = data["regressions"][0]
    assert row["signal_name"] == "Viewport regression: TaskCard at 375px"
    assert row["regression_class"] == "viewport_regression:TaskCard:overflow@375"
    assert row["severity"] == "critical"
    assert row["current_rate"] is None
    assert row["prior_rate"] is None
    assert row["resolved"] is False
    assert row["spawned_task_id"] is None
    assert row["viewport_component"] == "TaskCard"
    assert row["viewport_assertion_type"] == "overflow"
    assert row["viewport_width"] == 375


@pytest.mark.asyncio
async def test_pipeline_regressions_derives_viewport_metadata_from_failure_text(
    client,
    db_session,
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    failed = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=_utcnow() - timedelta(hours=1),
        latest_status_message=(
            "expectNoOverflowAt(375) failed: MessagesPanel overflows viewport"
        ),
        context={"failure_class": "VIEWPORT_REGRESSION"},
    )
    db_session.add(failed)
    await db_session.commit()

    resp = await client.get(f"/api/admin/pipeline-regressions?project_id={project.id}")
    assert resp.status_code == 200
    rows = resp.json()["regressions"]
    assert len(rows) == 1
    assert rows[0]["regression_class"] == "viewport_regression:MessagesPanel:overflow@375"
    assert rows[0]["viewport_component"] == "MessagesPanel"
    assert rows[0]["viewport_assertion_type"] == "overflow"
    assert rows[0]["viewport_width"] == 375


@pytest.mark.asyncio
async def test_pipeline_regressions_surfaces_viewport_row_when_metadata_missing(
    client,
    db_session,
):
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    failed = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=_utcnow() - timedelta(hours=1),
        latest_status_message="viewport assertion failed before screenshot metadata",
        context={"failure_category": "VIEWPORT_REGRESSION"},
    )
    db_session.add(failed)
    await db_session.commit()

    resp = await client.get(f"/api/admin/pipeline-regressions?project_id={project.id}")
    assert resp.status_code == 200
    rows = resp.json()["regressions"]
    assert len(rows) == 1
    assert rows[0]["signal_name"] == "Viewport regression: metadata unavailable"
    assert rows[0]["regression_class"] == f"viewport_regression:metadata_unavailable:{failed.id}"
    assert rows[0]["viewport_component"] is None
    assert rows[0]["viewport_assertion_type"] is None
    assert rows[0]["viewport_width"] is None


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
    # The endpoint stamps the single canonical churn direction so the widget
    # glyph and the narrative verdict read the same value rather than each
    # re-deriving it.
    assert data["churn_direction"] in {"falling", "rising", "flat"}


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


@pytest.mark.asyncio
async def test_pool_dispatch_funnel_trend_endpoint(client, db_session):
    """The pool-dispatch-funnel-trend endpoint returns an empty list before any
    snapshot lands, then one bucket per snapshot carrying the funnel aggregates
    and the dominant lease-miss reason; snapshots lacking the block are skipped."""
    from app.efficiency_snapshot import DESIGN_DOC_KEY

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()

    # Before any snapshot captures the block: empty buckets.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-dispatch-funnel-trend"
    )
    assert resp.status_code == 200
    assert resp.json()["buckets"] == []

    project.design_doc = {
        DESIGN_DOC_KEY: {
            "snapshots": [
                {"captured_at": "2026-05-30T00:00:00"},  # no block -> skipped
                {
                    "captured_at": "2026-05-31T00:00:00",
                    "pool_dispatch_funnel": {
                        "pool_lease_hits": 4,
                        "cold_dispatches": 6,
                        "pool_hit_rate": 0.4,
                        "lease_miss_total": 5,
                        "dominant_miss": {
                            "reason": "no_compatible_member",
                            "share": 0.6,
                        },
                    },
                },
            ]
        }
    }
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/projects/{project.id}/pool-dispatch-funnel-trend"
    )
    assert resp.status_code == 200
    buckets = resp.json()["buckets"]
    assert len(buckets) == 1
    assert buckets[0]["captured_at"] == "2026-05-31T00:00:00"
    assert buckets[0]["pool_lease_hits"] == 4
    assert buckets[0]["cold_dispatches"] == 6
    assert buckets[0]["pool_hit_rate"] == 0.4
    assert buckets[0]["lease_miss_total"] == 5
    assert buckets[0]["dominant_miss_reason"] == "no_compatible_member"
    assert buckets[0]["dominant_miss_share"] == 0.6


@pytest.mark.asyncio
async def test_pipeline_efficiency_includes_warm_start_origin_breakdown(
    client, db_session
):
    """/pipeline-efficiency emits a per-origin warm-start breakdown per window,
    bucketed by ``_timing.warm_claim_origin`` over the SAME tasks that produce
    ``warm_reuse_rate``."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    def _warm(origin):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_timing": {"warm_start": True, "warm_claim_origin": origin}},
        )

    # 2 pool, 1 backlog, 1 chain (next_role_endpoint) within the 24h window.
    db_session.add_all(
        [
            _warm("warm_pool"),
            _warm("warm_pool"),
            _warm("backlog_reuse"),
            _warm("next_role_endpoint"),
        ]
    )
    # 1 cold task: counts in the reuse denominator but not the warm breakdown.
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_timing": {"warm_start": False}},
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/pipeline-efficiency?project_id={project.id}")
    assert resp.status_code == 200
    bd = resp.json()["window_24h"]["warm_start_origin_breakdown"]
    assert bd is not None
    assert bd["warm_pool"] == 2
    assert bd["backlog_reuse"] == 1
    assert bd["chain_continuity"] == 1
    assert bd["warm_total"] == 4
    assert bd["pool_origin_share"] == round(2 / 4, 4)


@pytest.mark.asyncio
async def test_pipeline_efficiency_breakdown_none_when_no_warm_starts(
    client, db_session
):
    """With only cold tasks in the window, the breakdown is None (nothing to
    attribute) while warm_reuse_rate stays computable."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_timing": {"warm_start": False}},
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/pipeline-efficiency?project_id={project.id}")
    assert resp.status_code == 200
    win = resp.json()["window_24h"]
    assert win["warm_start_origin_breakdown"] is None
    assert win["warm_reuse_rate"] == 0.0


@pytest.mark.asyncio
async def test_ui_load_timing_round_trip(client, db_session):
    """POST records a sample; GET returns it under the correct surface key."""
    import app.routers.admin_pipeline_observability as obs_mod

    # Reset in-memory store before test to avoid cross-test pollution.
    with obs_mod._UI_TIMING_LOCK:
        obs_mod._UI_TIMING_SAMPLES.clear()

    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "chat", "page_ready_ms": 1234},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = await client.get("/api/admin/ui-load-timing")
    assert resp.status_code == 200
    data = resp.json()
    assert "chat" in data["surfaces"]
    samples = data["surfaces"]["chat"]
    assert len(samples) == 1
    assert samples[0]["page_ready_ms"] == 1234
    assert "ts" in samples[0]
    assert samples[0]["sample_type"] == "first_commit"


@pytest.mark.asyncio
async def test_ui_load_timing_sample_type_persisted(client, db_session):
    """sample_type is persisted and returned in GET; defaults to 'first_commit' when omitted."""
    import app.routers.admin_pipeline_observability as obs_mod

    with obs_mod._UI_TIMING_LOCK:
        obs_mod._UI_TIMING_SAMPLES.clear()

    # Post with explicit sample_type="data_ready"
    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "chat_timing", "page_ready_ms": 500, "sample_type": "data_ready"},
    )
    assert resp.status_code == 200

    # Post without sample_type — should default to "first_commit"
    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "chat_timing", "page_ready_ms": 600},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/admin/ui-load-timing")
    assert resp.status_code == 200
    samples = resp.json()["surfaces"]["chat_timing"]
    assert len(samples) == 2
    types = {s["sample_type"] for s in samples}
    assert "data_ready" in types
    assert "first_commit" in types


@pytest.mark.asyncio
async def test_ui_load_timing_rejects_invalid_ms(client, db_session):
    """page_ready_ms out of valid range returns 400."""
    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "pipeline", "page_ready_ms": -5},
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "pipeline", "page_ready_ms": 700_000},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ui_load_timing_get_empty(client, db_session):
    """GET returns empty surfaces dict when no samples have been recorded."""
    import app.routers.admin_pipeline_observability as obs_mod

    with obs_mod._UI_TIMING_LOCK:
        obs_mod._UI_TIMING_SAMPLES.clear()

    resp = await client.get("/api/admin/ui-load-timing")
    assert resp.status_code == 200
    assert resp.json()["surfaces"] == {}


@pytest.mark.asyncio
async def test_ui_load_timing_aggregates_persist_across_process_restart(
    client, db_session
):
    """Samples written to the DB survive an in-process ring-buffer reset.

    Simulates a deploy (process restart) by clearing the in-memory cache
    after recording samples.  The GET endpoint must still return the
    persisted rows because it reads from the DB, not the cache.
    """
    import app.routers.admin_pipeline_observability as obs_mod

    # Record two samples on different surfaces.
    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "chat", "page_ready_ms": 800},
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/api/admin/ui-load-timing",
        json={"surface": "pipeline", "page_ready_ms": 1500},
    )
    assert resp.status_code == 200

    # Simulate a process restart by wiping the in-memory cache.
    with obs_mod._UI_TIMING_LOCK:
        obs_mod._UI_TIMING_SAMPLES.clear()

    # GET must still return both samples from the database.
    resp = await client.get("/api/admin/ui-load-timing")
    assert resp.status_code == 200
    data = resp.json()
    assert "chat" in data["surfaces"]
    assert "pipeline" in data["surfaces"]
    assert data["surfaces"]["chat"][0]["page_ready_ms"] == 800
    assert data["surfaces"]["pipeline"][0]["page_ready_ms"] == 1500


@pytest.mark.asyncio
async def test_ui_load_timing_pinned_baseline_survives_sample_cap(
    client, db_session
):
    """A pinned baseline survives after enough new samples evict the originals.

    Records 10 samples, pins a baseline, then records 201 more samples to
    exceed the 200-per-surface FIFO cap.  The raw samples are trimmed but
    the baseline (p50/p90/count) remains intact in the GET response.
    """
    import app.routers.admin_pipeline_observability as obs_mod

    surface = "test_cap_survive"

    with obs_mod._UI_TIMING_LOCK:
        obs_mod._UI_TIMING_SAMPLES.clear()

    # Step 1: Record 10 baseline samples.
    expected_vals = sorted((i + 1) * 100 for i in range(10))  # 100, 200, ..., 1000
    for ms in expected_vals:
        resp = await client.post(
            "/api/admin/ui-load-timing",
            json={"surface": surface, "page_ready_ms": ms},
        )
        assert resp.status_code == 200

    # Step 2: Pin baseline.
    resp = await client.post(
        "/api/admin/ui-load-timing/pin-baseline",
        json={"label": "Before cap test"},
    )
    assert resp.status_code == 200
    pin_data = resp.json()
    assert pin_data["ok"] is True
    assert pin_data["surface_count"] >= 1
    baseline_id = None
    for b in pin_data["baselines"]:
        if b["surface"] == surface:
            baseline_id = b["id"]
            assert b["count"] == 10
            assert b["p50"] == 600  # sorted[5]
            assert b["p90"] == 1000  # sorted[9]
            assert b["label"] == "Before cap test"
            break
    assert baseline_id is not None, "Baseline not found for test surface"

    # Step 3: Record 201 more samples to exceed the 200-per-surface cap.
    for i in range(201):
        resp = await client.post(
            "/api/admin/ui-load-timing",
            json={"surface": surface, "page_ready_ms": 5000 + i},
        )
        assert resp.status_code == 200

    # Step 4: Verify raw samples are capped at 200 and originals evicted.
    resp = await client.get("/api/admin/ui-load-timing")
    assert resp.status_code == 200
    data = resp.json()
    assert surface in data["surfaces"]
    assert len(data["surfaces"][surface]) == 200
    # All surviving samples should be >= 5000 (the eviction batch).
    assert all(s["page_ready_ms"] >= 5000 for s in data["surfaces"][surface])

    # Step 5: Verify baseline still returns with correct stats.
    assert len(data["baselines"]) >= 1
    surviving = [b for b in data["baselines"] if b["surface"] == surface]
    assert len(surviving) >= 1
    bl = surviving[0]
    assert bl["count"] == 10
    assert bl["p50"] == 600
    assert bl["p90"] == 1000
    assert bl["label"] == "Before cap test"
    assert bl["id"] == baseline_id
