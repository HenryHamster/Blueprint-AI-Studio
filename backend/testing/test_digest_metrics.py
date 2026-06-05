"""Unit tests for digest metric helpers (app.digest_metrics / sr_review_quality_metrics)."""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.models import (
    TaskStatus,
    ReleaseVersion,
)
from app.digest_metrics import (
    _FAILURE_CLASS_LABELS,
    _compute_first_review_approval_breakdowns,
    _compute_non_code_outcomes,
    compute_churn_direction,
    compute_digest_cost_economics,
    compute_digest_trend_history,
    compute_failure_grouping,
    compute_narrative_summary,
    compute_recurring_error_clusters,
    compute_release_qualifier_infra_metrics,
    compute_retry_analytics,
    compute_scope_mismatch_summary,
    compute_task_type_cost_adherence,
)
from app.failure_classification import ALL_FAILURE_CLASSES
from app.sr_review_quality_metrics import (
    compute_review_quality_metrics,
    compute_sr_prediction_accuracy,
)
from testing.factories import make_project, make_task, _utcnow


def test_compute_failure_grouping_respects_persisted_verdict():
    """Persisted authoritative verdict is returned by compute_failure_grouping even
    without at_time, preventing RATE_LIMIT→CODE_ERROR drift for analytics paths."""
    from app.failure_classification import (
        RATE_LIMIT,
        persist_authoritative_verdict,
    )

    now = _utcnow()
    heartbeat = now - timedelta(hours=2)
    window_start = now - timedelta(hours=24)

    task = make_task(
        status=TaskStatus.FAILED,
        latest_status_message="claude_code_failed",
        context={"failure_category": "claude_code_failed"},
        last_heartbeat=heartbeat,
    )
    # Simulate the reaper persisting RATE_LIMIT at the failure instant.
    persist_authoritative_verdict(task, RATE_LIMIT)

    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=window_start,
        now=now,
    )
    groups = {g["failure_class"]: g for g in result["failure_groups"]}
    assert RATE_LIMIT in groups, (
        f"Expected RATE_LIMIT group from persisted verdict, got: {list(groups)}"
    )
    assert "CODE_ERROR" not in groups, (
        "classify_failure must not flip RATE_LIMIT→CODE_ERROR in analytics path"
    )


def _build_narrative_summary(**overrides):
    base = dict(
        hours=24,
        project_id=None,
        completed_count=0,
        failed_count=0,
        pending_approval_count=0,
        needs_attention=[],
        failure_groups=[],
        failure_taxonomy_distribution={
            "categories": {},
            "total_failures": 0,
            "dominant_category": None,
        },
        trend=None,
        completed_delta=0,
        prev_completed_count=0,
        sorted_completions=[],
        sorted_failures=[],
        direction_progress_list=[],
        running_count=0,
        queued_count=0,
        pr_count=0,
        error_log_map={},
        failed_tasks=[],
    )
    base.update(overrides)
    return compute_narrative_summary(**base)


def test_compute_narrative_summary_all_clear():
    completed = [make_task(title="Completed Alpha", status=TaskStatus.COMPLETED)]
    assert (
        _build_narrative_summary(
            completed_count=1,
            sorted_completions=completed,
            pr_count=1,
        )
        == "All clear — 1 task completed with 1 PR merged, no action needed."
    )


def test_compute_narrative_summary_approval_only():
    assert (
        _build_narrative_summary(
            pending_approval_count=1,
            needs_attention=[{"reason": "approval_required", "waiting_hours": 2}],
        )
        == "1 task awaiting your approval (2h)."
    )


def test_compute_narrative_summary_dominant_code_error_refined():
    failed_tasks = [
        make_task(
            title="Lint Fail 1",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_foo",
        ),
        make_task(
            title="Lint Fail 2",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_bar",
        ),
        make_task(
            title="Lint Fail 3",
            status=TaskStatus.FAILED,
            context={"failure_category": "test_failed"},
            latest_status_message="pytest failed on test_baz",
        ),
    ]
    assert (
        _build_narrative_summary(
            failed_count=5,
            failure_groups=[{"failure_class": "CODE_ERROR", "count": 3}],
            failure_taxonomy_distribution={
                "categories": {"review_quality": {"count": 1}},
                "total_failures": 1,
                "dominant_category": "review_quality",
            },
            failed_tasks=failed_tasks,
        )
        == "Most failures (3/5) are code errors from test/lint failures — strengthen self-review before PR creation."
    )


def test_compute_narrative_summary_no_dominant_failure_class():
    failed_tasks = [
        make_task(title="Fail A", status=TaskStatus.FAILED),
        make_task(title="Fail B", status=TaskStatus.FAILED),
    ]
    assert (
        _build_narrative_summary(
            failed_count=2,
            failure_groups=[{"failure_class": "CODE_ERROR", "count": 1}],
            failure_taxonomy_distribution={
                "categories": {"code_defect": {"count": 1}},
                "total_failures": 1,
                "dominant_category": "code_defect",
            },
            sorted_failures=failed_tasks,
            failed_tasks=failed_tasks,
        )
        == "2 tasks failed (Fail A, Fail B)."
    )


def test_compute_narrative_summary_completion_trend_prefix():
    completed = [
        make_task(title="Completed Alpha", status=TaskStatus.COMPLETED),
        make_task(title="Completed Beta", status=TaskStatus.COMPLETED),
    ]
    failed_tasks = [make_task(title="Failed Task", status=TaskStatus.FAILED)]
    assert _build_narrative_summary(
        completed_count=2,
        failed_count=1,
        failure_groups=[{"failure_class": "CODE_ERROR", "count": 1}],
        failure_taxonomy_distribution={
            "categories": {"code_defect": {"count": 1}},
            "total_failures": 1,
            "dominant_category": "code_defect",
        },
        trend="improving",
        completed_delta=1,
        prev_completed_count=1,
        sorted_completions=completed,
        sorted_failures=failed_tasks,
        failed_tasks=failed_tasks,
    ) == (
        "1 task failed (Failed Task). "
        "Good progress — 2 tasks completed including Completed Alpha, Completed Beta (up from 1 last period)."
    )


def test_compute_narrative_summary_direction_taxonomy_hint():
    assert (
        _build_narrative_summary(
            project_id=uuid4(),
            direction_progress_list=[
                {
                    "direction_text": "Team Alpha",
                    "prs_merged": 2,
                    "possibly_saturated": False,
                    "tasks_created": 0,
                    "taxonomy_summary": {"review_quality": 2, "code_defect": 1},
                }
            ],
        )
        == "Research: Team Alpha: 2 PRs merged (mostly review-quality failures)."
    )


def test_compute_narrative_summary_empty_window_fallback():
    assert _build_narrative_summary() == "All quiet — no activity in the last 24h."


def test_compute_narrative_summary_convergence_verdict_appears():
    """When the outer-loop window has churn signal (>=2 trend buckets) and a
    dominant blocker, the briefing states a falling/rising churn verdict and
    names the quantified blocker."""
    narrative = _build_narrative_summary(
        outer_loop_convergence_summary={
            "dominant_blocker": {
                "cause": "cap_exhausted",
                "count": 3,
                "total": 4,
                "share": 0.75,
            }
        },
        outer_loop_convergence_trend={
            "buckets": [
                {"incidence_counts": {"churn": 5}, "churn_total": 5},
                {"incidence_counts": {"churn": 2}, "churn_total": 2},
            ]
        },
    )
    assert narrative == (
        "Review/fix churn is falling; the main blocker is cap exhausted "
        "(3/4 non-converged PR chains)."
    )


def test_compute_narrative_summary_convergence_verdict_suppressed_thin_window():
    """A thin window (single trend bucket) carries no convergence signal, so the
    briefing emits no review/fix churn sentence."""
    narrative = _build_narrative_summary(
        outer_loop_convergence_summary={
            "dominant_blocker": {
                "cause": "cap_exhausted",
                "count": 3,
                "total": 4,
                "share": 0.75,
            }
        },
        outer_loop_convergence_trend={
            "buckets": [{"incidence_counts": {"churn": 5}, "churn_total": 5}]
        },
    )
    assert "churn" not in narrative.lower()
    assert narrative == "All quiet — no activity in the last 24h."


def test_compute_churn_direction_two_half_average():
    """Direction is decided by first-half vs second-half AVERAGE, not endpoints —
    a misleading endpoint spike does not flip an overall-falling window."""
    # Falling overall despite an 8-churn final spike (avg first half ≈ 8,
    # second half ≈ 4.67) — endpoints alone (10 vs 8) would read flat/falling.
    falling = [
        {"incidence_counts": {"churn": c}, "churn_total": c}
        for c in (10, 8, 6, 4, 2, 8)
    ]
    assert compute_churn_direction(falling) == "falling"
    # Rising overall despite a low final bucket (avg first half ≈ 5,
    # second half ≈ 7.3) — endpoints alone (2 vs 3) would read flat.
    rising = [
        {"incidence_counts": {"churn": c}, "churn_total": c}
        for c in (2, 8, 9, 10, 3)
    ]
    assert compute_churn_direction(rising) == "rising"
    # Genuinely stable churn reads flat.
    flat = [{"incidence_counts": {"churn": 5}, "churn_total": 5} for _ in range(3)]
    assert compute_churn_direction(flat) == "flat"
    # Fewer than two buckets carries no direction.
    assert compute_churn_direction([{"churn_total": 5}]) == "flat"
    # Falls back to churn_total when incidence_counts.churn is absent.
    assert (
        compute_churn_direction([{"churn_total": 9}, {"churn_total": 1}]) == "falling"
    )


def test_compute_narrative_summary_consumes_explicit_churn_direction():
    """When the trend carries a backend-stamped churn_direction, the narrative
    verdict uses it verbatim rather than re-deriving — so it can never disagree
    with the widget glyph that reads the same field."""
    narrative = _build_narrative_summary(
        outer_loop_convergence_summary={
            "dominant_blocker": {
                "cause": "cap_exhausted",
                "count": 3,
                "total": 4,
                "share": 0.75,
            }
        },
        outer_loop_convergence_trend={
            "churn_direction": "rising",
            # Raw endpoints (5 -> 2) would read falling; the stamped direction wins.
            "buckets": [
                {"incidence_counts": {"churn": 5}, "churn_total": 5},
                {"incidence_counts": {"churn": 2}, "churn_total": 2},
            ],
        },
    )
    assert narrative == (
        "Review/fix churn is rising; the main blocker is cap exhausted "
        "(3/4 non-converged PR chains)."
    )


def _make_failed_task(**kwargs):
    """Minimal failed Task object for compute_failure_grouping unit tests."""
    return make_task(status=TaskStatus.FAILED, **kwargs)


def _make_cost_task(task_type, cost, *, status=TaskStatus.COMPLETED, created_at=None):
    """Terminal task whose total VM cost equals ``cost`` (via banked spend).

    Banking the cost on ``vm_runtime_cost_banked_db`` and leaving
    ``vm_provisioned_at`` None gives a deterministic total cost equal to ``cost``
    (current-attempt term is 0), which is all the adherence math needs.
    """
    now = _utcnow()
    return make_task(
        status=status,
        payloads=[{"type": task_type}],
        created_at=created_at if created_at is not None else now - timedelta(hours=1),
        vm_runtime_cost_banked_db=cost,
    )


def test_cost_adherence_avg_p95_and_sample_count():
    """Per-type avg/p95/sample_count reflect the banked total VM cost."""
    now = _utcnow()
    tasks = [
        _make_cost_task("create", 2.0),
        _make_cost_task("create", 4.0),
        _make_cost_task("create", 6.0),
    ]
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=now - timedelta(days=14),
        ceilings={"create": 8.0},
    )
    row = next(r for r in rows if r["id"] == "create")
    assert row["sample_count"] == 3
    assert row["avg_cost_usd"] == pytest.approx(4.0)
    assert row["p95_cost_usd"] == pytest.approx(6.0)
    assert row["declared_ceiling_usd"] == 8.0
    assert row["over_ceiling"] is False


def test_cost_adherence_over_ceiling_requires_min_samples():
    """A type only flags over_ceiling once it has >= MIN_SAMPLES costly runs."""
    now = _utcnow()
    window_start = now - timedelta(days=14)
    # Two runs, both well over the $1 ceiling, but below the 3-sample minimum.
    thin = compute_task_type_cost_adherence(
        [_make_cost_task("fix", 5.0), _make_cost_task("fix", 5.0)],
        window_start=window_start,
        ceilings={"fix": 1.0},
    )
    assert next(r for r in thin if r["id"] == "fix")["over_ceiling"] is False

    # Three over-ceiling runs now trip the flag.
    enough = compute_task_type_cost_adherence(
        [_make_cost_task("fix", 5.0) for _ in range(3)],
        window_start=window_start,
        ceilings={"fix": 1.0},
    )
    assert next(r for r in enough if r["id"] == "fix")["over_ceiling"] is True


def test_cost_adherence_none_ceiling_never_flags():
    """A type with no declared ceiling never flags over_ceiling."""
    now = _utcnow()
    rows = compute_task_type_cost_adherence(
        [_make_cost_task("research", 99.0) for _ in range(5)],
        window_start=now - timedelta(days=14),
        ceilings={"research": None},
    )
    row = next(r for r in rows if r["id"] == "research")
    assert row["sample_count"] == 5
    assert row["over_ceiling"] is False


def test_cost_adherence_excludes_out_of_window_and_zero_cost():
    """Out-of-window tasks and tasks with no VM cost contribute no sample."""
    now = _utcnow()
    window_start = now - timedelta(days=14)
    tasks = [
        _make_cost_task("create", 3.0),  # in-window, counts
        _make_cost_task("create", 3.0, created_at=now - timedelta(days=30)),  # too old
        make_task(  # terminal but never ran a VM -> cost None
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=now - timedelta(hours=1),
        ),
    ]
    rows = compute_task_type_cost_adherence(
        tasks, window_start=window_start, ceilings={"create": 8.0}
    )
    assert next(r for r in rows if r["id"] == "create")["sample_count"] == 1


def test_cost_adherence_renders_row_per_registered_type():
    """Every registered type gets a row even with zero in-window samples."""
    now = _utcnow()
    rows = compute_task_type_cost_adherence(
        [],
        window_start=now - timedelta(days=14),
        ceilings={"create": 8.0, "fix": 3.0},
    )
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"create", "fix"}
    assert by_id["create"]["sample_count"] == 0
    assert by_id["create"]["avg_cost_usd"] is None
    assert by_id["create"]["p95_cost_usd"] is None
    assert by_id["create"]["over_ceiling"] is False


def test_cost_adherence_compute_substrate_sources_from_compute_jobs():
    """A compute-substrate type (compute_experiment) is measured from the
    compute-job ledger, not from Task VM fields."""
    now = _utcnow()
    window_start = now - timedelta(days=14)
    # Three compute_experiment tasks. Their banked VM cost is intentionally
    # different (and would mislead) — the honest cost comes from the compute-job
    # ledger keyed by task id.
    tasks = [_make_cost_task("compute_experiment", 0.01) for _ in range(3)]
    compute_job_cost_by_task = {
        str(tasks[0].id): 6.0,
        str(tasks[1].id): 8.0,
        str(tasks[2].id): 10.0,
    }
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=window_start,
        ceilings={"compute_experiment": 5.0},
        compute_job_cost_by_task=compute_job_cost_by_task,
    )
    row = next(r for r in rows if r["id"] == "compute_experiment")
    assert row["sample_count"] == 3
    assert row["avg_cost_usd"] == pytest.approx(8.0)
    assert row["p95_cost_usd"] == pytest.approx(10.0)
    # Average ($8) exceeds the $5 ceiling with >= MIN_SAMPLES runs.
    assert row["over_ceiling"] is True


def test_cost_adherence_compute_substrate_no_ledger_entry_no_sample():
    """A compute-substrate task with VM cost but no compute-job spend contributes
    no sample — it must NOT fall back to the monitoring VM's cost."""
    now = _utcnow()
    window_start = now - timedelta(days=14)
    # Banked VM cost present, but no entry (and a non-positive entry) in the
    # compute-job ledger -> no samples.
    tasks = [
        _make_cost_task("compute_experiment", 3.0),
        _make_cost_task("compute_experiment", 3.0),
    ]
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=window_start,
        ceilings={"compute_experiment": 5.0},
        compute_job_cost_by_task={str(tasks[1].id): 0.0},  # non-positive -> skip
    )
    row = next(r for r in rows if r["id"] == "compute_experiment")
    assert row["sample_count"] == 0
    assert row["avg_cost_usd"] is None
    assert row["over_ceiling"] is False


def test_cost_adherence_non_compute_type_still_uses_vm_cost():
    """Regression: non-compute types keep sourcing cost from VM fields and ignore
    the compute-job ledger."""
    now = _utcnow()
    window_start = now - timedelta(days=14)
    tasks = [_make_cost_task("create", 4.0) for _ in range(3)]
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=window_start,
        ceilings={"create": 8.0},
        # A stray compute-job entry for a create task must be ignored.
        compute_job_cost_by_task={str(tasks[0].id): 999.0},
    )
    row = next(r for r in rows if r["id"] == "create")
    assert row["sample_count"] == 3
    assert row["avg_cost_usd"] == pytest.approx(4.0)
    assert row["over_ceiling"] is False


def test_compute_failure_grouping_empty():
    """Empty failed-task list returns zeroed-out structure."""
    now = _utcnow()
    result = compute_failure_grouping(
        failed_tasks=[],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert result["failure_groups"] == []
    assert result["cost_by_failure_class"] == {}
    assert result["total_failed_cost_usd"] == 0.0
    assert result["provision_waste_cost_usd"] == 0.0
    assert result["failure_stage_counts"] == {}
    assert result["analysis_overflow_summary"]["total_count"] == 0
    assert result["provision_waste_by_direction"] == []


def test_compute_failure_grouping_rate_limit_class():
    """Task with a rate-limit error message is placed in the RATE_LIMIT failure group."""
    now = _utcnow()
    task = _make_failed_task(
        title="Rate Limited Task",
        latest_status_message="API Error: Rate limit reached",
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={str(task.id): "API Error: Rate limit reached"},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    classes = {g["failure_class"] for g in result["failure_groups"]}
    assert "RATE_LIMIT" in classes
    rate_group = next(
        g for g in result["failure_groups"] if g["failure_class"] == "RATE_LIMIT"
    )
    assert rate_group["count"] == 1
    assert rate_group["tasks"][0]["title"] == "Rate Limited Task"
    assert rate_group["label"] == "Rate Limit"


def test_compute_failure_grouping_provision_failure_waste():
    """PROVISION_FAILURE tasks contribute to provision_waste_cost_usd."""
    now = _utcnow()
    prov_at = now - timedelta(hours=1)
    stop_at = now - timedelta(minutes=30)
    task = _make_failed_task(
        title="Quota Exceeded Task",
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=False,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert result["provision_waste_cost_usd"] > 0.0
    assert "PROVISION_FAILURE" in result["cost_by_failure_class"]
    assert result["provision_waste_cost_usd"] == pytest.approx(
        result["cost_by_failure_class"]["PROVISION_FAILURE"]
    )


def test_compute_failure_grouping_sorted_by_count_descending():
    """failure_groups are sorted by count descending."""
    now = _utcnow()
    tasks = [
        _make_failed_task(
            title=f"Code Error {i}",
            latest_status_message="TypeError: something broke",
        )
        for i in range(3)
    ] + [
        _make_failed_task(
            title="Rate Task",
            latest_status_message="API Error: Rate limit reached",
        )
    ]
    result = compute_failure_grouping(
        failed_tasks=tasks,
        error_log_map={str(t.id): t.latest_status_message for t in tasks},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    counts = [g["count"] for g in result["failure_groups"]]
    assert counts == sorted(counts, reverse=True)
    assert result["failure_groups"][0]["count"] >= 3


def test_compute_failure_grouping_provision_waste_by_direction():
    """provision_waste_by_direction groups PROVISION_FAILURE cost by direction_id."""
    import uuid

    now = _utcnow()
    dir_id = str(uuid.uuid4())
    prov_at = now - timedelta(hours=1)
    stop_at = now - timedelta(minutes=30)
    task = _make_failed_task(
        title="Provision Fail",
        latest_status_message="CPUS_ALL_REGIONS quota exceeded for region us-central1",
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=False,
        context={"direction_id": dir_id},
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={dir_id: "My Direction"},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    breakdown = result["provision_waste_by_direction"]
    assert len(breakdown) == 1
    assert breakdown[0]["direction_id"] == dir_id
    assert breakdown[0]["direction_text"] == "My Direction"
    assert breakdown[0]["provision_cost_usd"] > 0.0
    assert breakdown[0]["provision_task_count"] == 1


def test_compute_failure_grouping_analysis_overflow_summary():
    """Tasks with scope_gate_triggered='analysis_overflow' appear in analysis_overflow_summary."""
    now = _utcnow()
    prov_at = now - timedelta(minutes=20)
    stop_at = now - timedelta(minutes=10)
    task = _make_failed_task(
        title="Overflow Task",
        context={"scope_gate_triggered": "analysis_overflow", "scope": "medium"},
        vm_provisioned_at=prov_at,
        vm_stopped_at=stop_at,
        vm_spot=True,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    summary = result["analysis_overflow_summary"]
    assert summary["total_count"] == 1
    assert summary["by_scope"].get("medium") == 1
    assert summary["cost_usd"] > 0.0


def test_compute_failure_grouping_task_entry_fields():
    """Each task entry in failure_groups contains the expected keys."""
    now = _utcnow()
    task = _make_failed_task(
        title="Code Error Task",
        latest_status_message="TypeError: cannot read property",
        attempt_count=2,
    )
    result = compute_failure_grouping(
        failed_tasks=[task],
        error_log_map={str(task.id): "TypeError: cannot read property"},
        dir_id_to_text={},
        window_start=now - timedelta(hours=24),
        now=now,
    )
    assert len(result["failure_groups"]) == 1
    entry = result["failure_groups"][0]["tasks"][0]
    assert entry["task_id"] == str(task.id)
    assert entry["title"] == "Code Error Task"
    assert entry["attempt_count"] == 2
    assert entry["error_snippet"] == "TypeError: cannot read property"
    assert "taxonomy" in entry
    assert "timeout_subtype" in entry
    assert entry["timeout_subtype"] is None  # not AGENT_TIMEOUT


def test_failure_grouping_labels_cover_all_canonical_failure_classes():
    """Digest failure-group labels stay aligned with the canonical taxonomy."""
    assert set(ALL_FAILURE_CLASSES) <= set(_FAILURE_CLASS_LABELS)
    assert all(_FAILURE_CLASS_LABELS[cls] != cls for cls in ALL_FAILURE_CLASSES)


def test_compute_recurring_error_clusters_empty():
    """Empty failed-task list returns an empty cluster list."""
    assert compute_recurring_error_clusters(failed_tasks=[], error_log_map={}) == []


def test_compute_recurring_error_clusters_excludes_singletons():
    """Clusters with count < 2 are excluded."""
    tasks = [
        _make_failed_task(latest_status_message="TypeError: foo is undefined"),
        _make_failed_task(latest_status_message="Rate limit exceeded"),
        _make_failed_task(latest_status_message="SyntaxError: unexpected token"),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert result == []


def test_compute_recurring_error_clusters_groups_by_fingerprint():
    """Failures sharing a normalized fingerprint group into one cluster, aggregating directions/task_types."""
    tasks = [
        _make_failed_task(
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-a"},
            latest_status_message="Merge conflict on PR #1234",
        ),
        _make_failed_task(
            payloads=[{"type": "fix"}],
            context={"direction_id": "dir-a"},
            latest_status_message="Merge conflict on PR #5678",
        ),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 1
    cluster = result[0]
    assert cluster["count"] == 2
    assert len(cluster["fingerprint"]) == 12
    assert "Merge conflict" in cluster["error_snippet"]
    assert cluster["direction_ids"] == ["dir-a"]
    assert sorted(cluster["task_types"]) == ["create", "fix"]
    assert cluster["first_task_id"] == str(tasks[0].id)


def test_compute_recurring_error_clusters_error_log_map_overrides_status_message():
    """When error_log_map has an entry for the task id, it takes precedence over latest_status_message."""
    task_a = _make_failed_task(latest_status_message="stale")
    task_b = _make_failed_task(latest_status_message="stale")
    result = compute_recurring_error_clusters(
        failed_tasks=[task_a, task_b],
        error_log_map={
            str(task_a.id): "Merge conflict on PR #1",
            str(task_b.id): "Merge conflict on PR #2",
        },
    )
    assert len(result) == 1
    assert "Merge conflict" in result[0]["error_snippet"]


def test_compute_recurring_error_clusters_sorted_by_count_descending_and_capped():
    """Returned clusters are sorted by count descending and capped at 5."""
    tasks = []
    # Build 6 distinct clusters with counts 7,6,5,4,3,2 — all should make it past the >=2 gate,
    # but only the top 5 should be returned (sorted desc).
    for cluster_idx, count in enumerate([7, 6, 5, 4, 3, 2]):
        for _ in range(count):
            tasks.append(
                _make_failed_task(
                    latest_status_message=f"Cluster {cluster_idx} unique-marker error",
                )
            )
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 5
    counts = [c["count"] for c in result]
    assert counts == sorted(counts, reverse=True)
    assert counts == [7, 6, 5, 4, 3]


def test_compute_recurring_error_clusters_skips_tasks_with_no_message():
    """Tasks with neither error_log_map entry nor latest_status_message are skipped."""
    tasks = [
        _make_failed_task(latest_status_message=""),
        _make_failed_task(latest_status_message=""),
        _make_failed_task(latest_status_message="Merge conflict on PR #1"),
        _make_failed_task(latest_status_message="Merge conflict on PR #2"),
    ]
    result = compute_recurring_error_clusters(
        failed_tasks=tasks,
        error_log_map={},
    )
    assert len(result) == 1
    assert result[0]["count"] == 2


def test_compute_digest_trend_history_length():
    """Returns exactly 7 entries regardless of task list."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    assert len(result) == 7


def test_compute_digest_trend_history_entry_keys():
    """Each entry contains the expected keys."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    for entry in result:
        assert "period_end" in entry
        assert "success_rate" in entry
        assert "completed" in entry
        assert "failed" in entry
        assert "failed_by_class" in entry
        assert "failed_by_taxonomy" in entry
        assert "period_cost_usd" in entry
        assert "first_attempt_success_rate" in entry
        assert "first_attempt_sample_size" in entry


def test_compute_digest_trend_history_empty_tasks():
    """With no tasks all counts are 0 and rates are None."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    for entry in result:
        assert entry["completed"] == 0
        assert entry["failed"] == 0
        assert entry["success_rate"] is None
        assert entry["first_attempt_success_rate"] is None
        assert entry["first_attempt_sample_size"] == 0


def test_compute_digest_trend_history_completed_task_in_last_period():
    """A COMPLETED task in the most recent period appears in the last entry."""
    now = _utcnow()
    task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[task], hours=24, now=now)
    last = result[-1]
    assert last["completed"] == 1
    assert last["failed"] == 0
    assert last["success_rate"] == 1.0


def test_compute_digest_trend_history_failed_task_classified():
    """A FAILED task is reflected in failed_by_class for the correct period."""
    now = _utcnow()
    task = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        latest_status_message="API Error: Rate limit reached",
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[task], hours=24, now=now)
    last = result[-1]
    assert last["failed"] == 1
    assert last["success_rate"] == 0.0
    assert isinstance(last["failed_by_class"], dict)
    assert isinstance(last["failed_by_taxonomy"], dict)


def test_compute_digest_trend_history_oldest_first():
    """Entries are in ascending chronological order (period_end increases)."""
    now = _utcnow()
    result = compute_digest_trend_history(all_tasks=[], hours=24, now=now)
    period_ends = [entry["period_end"] for entry in result]
    assert period_ends == sorted(period_ends)


def test_compute_digest_trend_history_first_attempt_rate():
    """first_attempt_success_rate reflects _first_attempt_succeeded context flag."""
    now = _utcnow()
    t_pass = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"_first_attempt_succeeded": True},
        created_at=now - timedelta(hours=1),
    )
    t_fail = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"_first_attempt_succeeded": False},
        created_at=now - timedelta(hours=1),
    )
    result = compute_digest_trend_history(all_tasks=[t_pass, t_fail], hours=24, now=now)
    last = result[-1]
    assert last["first_attempt_sample_size"] == 2
    assert last["first_attempt_success_rate"] == 0.5


def test_compute_digest_cost_economics_empty():
    """Empty tasks and directions return a zeroed-out cost structure."""
    now = _utcnow()
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=[],
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=12.5,
    )
    assert result["total_estimated_cost_usd"] == 0.0
    assert result["total_spot_cost_usd"] == 0.0
    assert result["total_on_demand_cost_usd"] == 0.0
    assert result["provisioning_overhead_usd"] == 0.0
    assert result["provisioning_overhead_pct"] is None
    assert result["daily_cost_usd"] == 0.0
    assert result["daily_cost_ceiling_usd"] == 12.5
    assert result["overall_product_ratio"] is None
    assert result["median_cost_per_merged_pr"] is None
    assert result["cost_per_merge_by_type"] == {}
    assert result["median_cpm_by_type"] is None
    assert result["pipeline_acceptance_rate"] is None
    assert result["pipeline_conversion_rate"] is None
    assert result["pipeline_autonomous_prs_merged"] == 0
    assert result["pipeline_assisted_prs_merged"] == 0
    assert result["pipeline_operator_action_count"] == 0
    assert result["pipeline_generalized_successes"] == 0
    assert result["pipeline_generalized_health_rate"] is None
    assert result["pipeline_outcome_kind_breakdown"] == {}
    assert result["overall_type_distribution"] == {}
    assert result["pipeline_filter_funnel"] == {}


def test_compute_digest_cost_economics_spot_on_demand_split():
    """Spot and on-demand task costs are split and totaled with known VM timing."""
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    spot_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),  # 1800s
    )
    od_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix"}],
        vm_spot=False,
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1),  # 3600s
    )
    result = compute_digest_cost_economics(
        all_tasks=[spot_task, od_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    expected_spot = round(SPOT_RATE * 1800, 2)
    expected_od = round(ON_DEMAND_RATE * 3600, 2)
    assert result["total_spot_cost_usd"] == expected_spot
    assert result["total_on_demand_cost_usd"] == expected_od
    # Both tasks are undirected (no direction_id) and inside the window.
    assert result["total_estimated_cost_usd"] == round(
        SPOT_RATE * 1800 + ON_DEMAND_RATE * 3600, 2
    )


def test_compute_digest_cost_economics_provisioning_overhead():
    """Time between provision and agent claim counts as provisioning overhead."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    # Provisioned 1h before now, claimed (started) 50m before → 600s overhead,
    # stopped 30m before → 1800s total compute.
    task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=False,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_started_at=now - timedelta(minutes=50),
        vm_stopped_at=now - timedelta(minutes=30),
    )
    result = compute_digest_cost_economics(
        all_tasks=[task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # overhead_raw = ON_DEMAND_RATE * 600 = 0.0111…; rounds to 0.01.
    assert result["provisioning_overhead_usd"] == 0.01
    # pct = round(overhead_raw / rounded_total_compute, 3) = round(0.01117/0.03, 3).
    assert result["provisioning_overhead_pct"] == 0.372


def test_compute_digest_cost_economics_threshold_flag_in_place():
    """cost_per_merge_exceeds_threshold is stamped onto direction dicts in place."""
    now = _utcnow()
    dirs = [
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 1.0},
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 2.0},
        {"estimated_cost_usd": 0.0, "cost_per_merged_pr": 100.0},
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # median([1, 2, 100]) = 2.0; threshold = 2.0 * 3 = 6.0.
    assert result["median_cost_per_merged_pr"] == 2.0
    # In-place mutation on the same dict objects.
    assert dirs[0]["cost_per_merge_exceeds_threshold"] is False
    assert dirs[1]["cost_per_merge_exceeds_threshold"] is False
    assert dirs[2]["cost_per_merge_exceeds_threshold"] is True


def test_compute_digest_cost_economics_cost_per_merge_by_type():
    """Per-task-type cost-per-merge counts merged tasks by payload type."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    merged = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )
    unmerged = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={},
    )
    result = compute_digest_cost_economics(
        all_tasks=[merged, unmerged],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    by_type = result["cost_per_merge_by_type"]
    assert "create" in by_type
    assert by_type["create"]["merged_count"] == 1
    assert by_type["create"]["cost_per_merge"] is not None
    assert by_type["create"]["total_cost_usd"] > 0.0


def test_compute_digest_cost_economics_excludes_non_merge_unblocks():
    """Cost-per-merge denominators ignore merge-equivalent non-landings."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    landed = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True, "landed_on_integration": True},
    )
    staging_only = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(hours=1, minutes=30),
        context={"pr_merged": True, "landed_on_integration": False},
    )
    result = compute_digest_cost_economics(
        all_tasks=[landed, staging_only],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    by_type = result["cost_per_merge_by_type"]
    assert by_type["create"]["merged_count"] == 1


def test_first_review_approval_breakdowns_exclude_non_merge_unblocks():
    """Review-eligible merge analytics require an integration-landing signal."""
    now = _utcnow()
    result = _compute_first_review_approval_breakdowns(
        [
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={"pr_merged": True, "landed_on_integration": True},
                created_at=now - timedelta(hours=1),
            ),
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create"}],
                context={"pr_merged": True, "landed_on_integration": False},
                created_at=now - timedelta(hours=1),
            ),
        ]
    )

    assert result["overall"] == 1.0
    assert result["sample_size"] == 1


def test_compute_digest_cost_economics_pipeline_aggregates():
    """Pipeline-level rates and counts aggregate across all directions."""
    now = _utcnow()
    dirs = [
        {
            "estimated_cost_usd": 0.0,
            "prs_merged": 3,
            "prs_rejected": 1,
            "tasks_created": 10,
            "autonomous_prs_merged": 2,
            "assisted_prs_merged": 1,
            "operator_action_count": 4,
            "generalized_successes": 5,
            "generalized_terminal_outcomes": 6,
            "outcome_kind_breakdown": {
                "code_merge": {"success": 3, "neutral": 0, "failure": 1},
                "research_quality": {"success": 2, "neutral": 0, "failure": 0},
            },
            "type_distribution": {"create": 5},
            "filter_funnel": {"a": 2},
        },
        {
            "estimated_cost_usd": 0.0,
            "prs_merged": 1,
            "prs_rejected": 1,
            "tasks_created": 5,
            "autonomous_prs_merged": 1,
            "assisted_prs_merged": 0,
            "operator_action_count": 1,
            "generalized_successes": 1,
            "generalized_terminal_outcomes": 2,
            "outcome_kind_breakdown": {
                "code_merge": {"success": 1, "neutral": 0, "failure": 1}
            },
            "type_distribution": {"create": 2, "fix": 1},
            "filter_funnel": {"a": 1, "b": 3},
        },
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # merged=4, rejected=2 → 4/6 = 0.667; created=15 → 4/15 = 0.267.
    assert result["pipeline_acceptance_rate"] == 0.667
    assert result["pipeline_conversion_rate"] == 0.267
    assert result["pipeline_autonomous_prs_merged"] == 3
    assert result["pipeline_assisted_prs_merged"] == 1
    assert result["pipeline_operator_action_count"] == 5
    assert result["pipeline_generalized_successes"] == 6
    assert result["pipeline_generalized_health_rate"] == 0.75
    assert result["pipeline_outcome_kind_breakdown"] == {
        "code_merge": {"success": 4, "neutral": 0, "failure": 2},
        "research_quality": {"success": 2, "neutral": 0, "failure": 0},
    }
    assert result["overall_type_distribution"] == {"create": 7, "fix": 1}
    assert result["pipeline_filter_funnel"] == {"a": 3, "b": 3}


def test_compute_scope_mismatch_summary_no_calibrated_tasks():
    """Returns None when no terminal task carries scope_calibrated_from."""
    completed = [make_task(status=TaskStatus.COMPLETED, context={})]
    failed = [make_task(status=TaskStatus.FAILED, context={"direction_id": "d-1"})]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is None


def test_compute_scope_mismatch_summary_empty_inputs():
    """Empty task lists yield None."""
    result = compute_scope_mismatch_summary(
        completed_tasks=[], failed_tasks=[], dir_id_to_text={}
    )
    assert result is None


def test_compute_scope_mismatch_summary_success_rate_and_total():
    """total_calibrated counts all calibrated tasks; success rate uses COMPLETED."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED, context={"scope_calibrated_from": "medium"}
        ),
        make_task(
            status=TaskStatus.COMPLETED, context={"scope_calibrated_from": "small"}
        ),
    ]
    failed = [
        make_task(status=TaskStatus.FAILED, context={"scope_calibrated_from": "large"}),
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is not None
    assert result["total_calibrated"] == 3
    assert result["calibrated_success_rate"] == round(2 / 3, 3)


def test_compute_scope_mismatch_summary_by_original_scope_breakdown():
    """by_original_scope buckets by the prefix before ':' and tallies outcomes."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "medium:reason-text"},
        ),
    ]
    failed = [
        make_task(
            status=TaskStatus.FAILED,
            context={"scope_calibrated_from": "medium:other-reason"},
        ),
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=failed, dir_id_to_text={}
    )
    assert result is not None
    assert result["by_original_scope"] == {
        "medium": {"total": 2, "completed": 1, "failed": 1}
    }


def test_compute_scope_mismatch_summary_blank_original_scope_is_unknown():
    """An empty scope_calibrated_from value is not calibrated; a bare ':' → 'unknown'."""
    # Empty string is falsy → excluded from calibrated set entirely.
    excluded = make_task(
        status=TaskStatus.COMPLETED, context={"scope_calibrated_from": ""}
    )
    # A value with no scope prefix before ':' falls back to "unknown".
    bare = make_task(
        status=TaskStatus.COMPLETED, context={"scope_calibrated_from": ":reason"}
    )
    result = compute_scope_mismatch_summary(
        completed_tasks=[excluded, bare], failed_tasks=[], dir_id_to_text={}
    )
    assert result is not None
    assert result["total_calibrated"] == 1
    assert "unknown" in result["by_original_scope"]


def test_compute_scope_mismatch_summary_top_directions_ranking():
    """top_directions ranks by calibration_rate, attaches text, and caps at 3."""
    completed = []
    failed = []
    # d-hi: 2/2 calibrated → rate 1.0
    for _ in range(2):
        completed.append(
            make_task(
                status=TaskStatus.COMPLETED,
                context={"scope_calibrated_from": "small", "direction_id": "d-hi"},
            )
        )
    # d-lo: 1/3 calibrated → rate ~0.333
    completed.append(
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "small", "direction_id": "d-lo"},
        )
    )
    for _ in range(2):
        failed.append(
            make_task(status=TaskStatus.FAILED, context={"direction_id": "d-lo"})
        )
    result = compute_scope_mismatch_summary(
        completed_tasks=completed,
        failed_tasks=failed,
        dir_id_to_text={"d-hi": "High dir", "d-lo": "Low dir"},
    )
    assert result is not None
    top = result["top_directions"]
    assert [d["direction_id"] for d in top] == ["d-hi", "d-lo"]
    assert top[0]["calibration_rate"] == 1.0
    assert top[0]["total_tasks"] == 2
    assert top[0]["direction_text"] == "High dir"
    assert top[1]["calibration_rate"] == round(1 / 3, 3)


def test_compute_scope_mismatch_summary_top_directions_capped_at_three():
    """Only the top 3 directions are returned even when more are calibrated."""
    completed = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={"scope_calibrated_from": "small", "direction_id": f"d-{i}"},
        )
        for i in range(5)
    ]
    result = compute_scope_mismatch_summary(
        completed_tasks=completed, failed_tasks=[], dir_id_to_text={}
    )
    assert result is not None
    assert len(result["top_directions"]) == 3


def _make_sr_task(check_results=None, rejection_categories=None, **kwargs):
    """Minimal terminal Task carrying _self_review_outcome.check_results and
    _first_attempt_rejection_categories for SR-prediction unit tests."""
    ctx = {
        "_self_review_outcome": {"check_results": check_results or {}},
        "_first_attempt_rejection_categories": rejection_categories or {},
    }
    ctx.update(kwargs.pop("context", {}))
    return make_task(status=TaskStatus.COMPLETED, context=ctx, **kwargs)


def test_compute_sr_prediction_accuracy_under_sample_size_returns_none():
    """Sample size <5 short-circuits to None regardless of content."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": False}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(4)
    ]
    assert compute_sr_prediction_accuracy(tasks) is None


def test_compute_sr_prediction_accuracy_perfect_match():
    """Every SR-failed category matches human rejection → hit rate 1.0, miss 0.0."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": False}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    assert result["prediction_hit_rate"] == 1.0
    assert result["miss_rate"] == 0.0
    assert result["false_alarm_rate"] == 0.0
    assert result["sample_size"] == 5
    # SR flagged AND human rejected => "self_flagged shipped unfixed"
    assert result["self_flagged_shipped_unfixed_task_count"] == 5
    assert result["self_flagged_shipped_unfixed_task_rate"] == 1.0


def test_compute_sr_prediction_accuracy_all_miss():
    """SR passed everything but humans rejected → miss_rate 1.0, hit_rate 0.0."""
    tasks = [
        _make_sr_task(
            check_results={"correctness": {"passed": True}},
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    assert result["prediction_hit_rate"] == 0.0
    assert result["miss_rate"] == 1.0
    # No SR failures recorded so false_alarm_rate is None (divide-by-zero guard).
    assert result["false_alarm_rate"] is None
    assert result["self_flagged_shipped_unfixed_task_count"] == 0
    assert result["self_flagged_shipped_unfixed_task_rate"] == 0.0


def test_compute_sr_prediction_accuracy_reviewer_origin_excluded():
    """Reviewer-stamped check_results entries do not count as author SR failures."""
    tasks = [
        _make_sr_task(
            check_results={
                "correctness": {
                    "passed": False,
                    "origin": "reviewer_recovery_after_incomplete_sr",
                }
            },
            rejection_categories={"correctness": 1},
        )
        for _ in range(5)
    ]
    result = compute_sr_prediction_accuracy(tasks)
    assert result is not None
    # The reviewer-origin failure should NOT count as an author catch.
    assert result["prediction_hit_rate"] == 0.0
    assert result["miss_rate"] == 1.0


def test_compute_review_quality_metrics_empty_returns_nones():
    """Empty input → every metric is None / empty-ish per sample-size guards."""
    result = compute_review_quality_metrics([])
    assert result["self_review_effectiveness"] is None
    assert result["self_review_prediction_accuracy"] is None
    assert result["review_accuracy"] is None
    # _compute_fresh_reviewer_effectiveness always returns a dict; we just check shape.
    assert isinstance(result["fresh_reviewer_effectiveness"], dict)


def test_compute_review_quality_metrics_self_review_effectiveness_shape():
    """≥3 tasks with _self_review_outcome populates self_review_effectiveness."""
    with_sr_clean = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"fixes_applied": False},
                "_first_attempt_succeeded": True,
            },
        )
        for _ in range(3)
    ]
    result = compute_review_quality_metrics(with_sr_clean)
    eff = result["self_review_effectiveness"]
    assert eff is not None
    assert eff["sample_size"] == 3
    assert eff["sr_clean_count"] == 3
    assert eff["sr_fixed_count"] == 0
    assert eff["with_self_review_approval_rate"] == 1.0
    assert eff["clean_verdict_approval_rate"] == 1.0


def test_compute_review_quality_metrics_review_accuracy_shape():
    """≥5 review tasks with a verdict populates review_accuracy."""
    review_tasks = []
    for _ in range(5):
        review_tasks.append(
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review"}],
                context={
                    "blueprint_review_verdict": "APPROVE",
                    "user_caught_issues": False,
                    "user_overrode_review": False,
                    "review_dismissed": False,
                },
            )
        )
    result = compute_review_quality_metrics(review_tasks)
    acc = result["review_accuracy"]
    assert acc is not None
    assert acc["total_reviews"] == 5
    assert acc["total_approvals"] == 5
    assert acc["overrides"] == 0
    assert acc["precision"] == 1.0
    assert acc["recall"] == 1.0


def test_parallel_review_effectiveness_buckets_correctly():
    """Tasks with review_was_parallelized=True/False bucket into parallel/serial."""

    def _make_review(parallelized: bool, verdict: str = "APPROVE"):
        return make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": verdict,
                "review_was_parallelized": parallelized,
            },
        )

    parallel_tasks = [_make_review(True) for _ in range(3)]
    serial_tasks = [_make_review(False) for _ in range(3)]
    result = compute_review_quality_metrics(parallel_tasks + serial_tasks)
    pre = result["parallel_review_effectiveness"]
    assert pre is not None
    assert pre["sample_sizes"]["parallel"] == 3
    assert pre["sample_sizes"]["serial"] == 3
    assert pre["parallel"]["count"] == 3
    assert pre["serial"]["count"] == 3
    assert pre["parallel"]["approval_rate"] == 1.0
    assert pre["serial"]["approval_rate"] == 1.0


def test_parallel_review_effectiveness_none_when_insufficient_sample():
    """Returns None when either bucket has fewer than 3 tasks."""

    def _make_review(parallelized: bool):
        return make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "APPROVE",
                "review_was_parallelized": parallelized,
            },
        )

    # Only 2 parallel tasks — below threshold
    tasks = [_make_review(True) for _ in range(2)] + [
        _make_review(False) for _ in range(5)
    ]
    result = compute_review_quality_metrics(tasks)
    assert result["parallel_review_effectiveness"] is None


# ─────────────────────────────────────────────────────────────────────────────
# _compute_non_code_outcomes — generalized outcomes for tasks outside PR chains
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_non_code_outcomes_mixed_portfolio():
    """Research/audit/memo successes (no PR chain) are surfaced by kind."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    in_window = now - timedelta(hours=1)

    research = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        plan_output=[{"title": "Investigate", "actionability_grade": "HIGH"}],
        created_at=in_window,
    )
    audit = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "security_audit"}],
        plan_output={"findings": [{"severity": "high"}]},
        created_at=in_window,
    )
    memo = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "memo"}],
        plan_output={"memo": "Reasoning"},
        created_at=in_window,
    )

    result = _compute_non_code_outcomes([research, audit, memo], window_start)
    assert result["total"] == 3
    assert result["successes"] == 3
    assert result["neutrals"] == 0
    assert result["failures"] == 0
    assert result["by_kind"]["research_quality"]["success"] == 1
    assert result["by_kind"]["audit_findings"]["success"] == 1
    assert result["by_kind"]["memo_artifact"]["success"] == 1


def test_compute_non_code_outcomes_excludes_review_chain_and_inflight():
    """Review-chain tasks and in-flight (non-terminal) tasks are excluded so
    neutrals/total are not inflated."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    in_window = now - timedelta(hours=1)

    # Part of a PR review chain → excluded by review_loop context.
    review_chain = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"review_loop": {"iteration": 1}},
        created_at=in_window,
    )
    # In-flight non-review task → must NOT be counted as a neutral.
    inflight = make_task(
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
        created_at=in_window,
    )
    # One genuine terminal success keeps the dict non-empty.
    audit = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "security_audit"}],
        plan_output={"findings": [{"severity": "low"}]},
        created_at=in_window,
    )

    result = _compute_non_code_outcomes([review_chain, inflight, audit], window_start)
    # Only the audit is counted: review_chain excluded, inflight excluded.
    assert result["total"] == 1
    assert result["successes"] == 1
    assert result["neutrals"] == 0


def test_compute_non_code_outcomes_empty_when_no_qualifying_tasks():
    """Returns an empty dict when only review-chain/in-flight tasks exist."""
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    inflight = make_task(
        status=TaskStatus.QUEUED,
        payloads=[{"type": "memo"}],
        created_at=now - timedelta(hours=1),
    )
    assert _compute_non_code_outcomes([inflight], window_start) == {}


class TestComputeRetryAnalytics:
    """Unit tests for the extracted compute_retry_analytics helper."""

    def _make_task(self, **kwargs):
        """Minimal Task-like object for unit tests (no DB needed)."""
        t = make_task(**kwargs)
        return t

    def test_no_retry_history(self):
        """Tasks with _attempt_number <= 1 (or absent) produce zero/None counts."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        failed = [
            make_task(status=TaskStatus.FAILED, context={}),
            make_task(status=TaskStatus.FAILED, context={"_attempt_number": 1}),
        ]
        completed = [
            make_task(status=TaskStatus.COMPLETED, context={}),
        ]
        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        assert result["retry_task_count"] == 0
        assert result["retry_success_count"] == 0
        assert result["retry_overhead_cost_usd"] == 0.0
        assert result["retry_recovery_cost_usd"] == 0.0
        assert result["retry_waste_by_direction"] == []
        assert result["avg_retries_before_failure"] is None
        assert result["retry_success_rate_by_class"] == {}
        assert result["retry_wasted_rate"] is None

    def test_retried_and_succeeded(self):
        """Completed tasks with _attempt_number > 1 count toward retry_success_count
        and retry_recovery_cost_usd; retry_success_rate_by_class is populated."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        completed = [
            make_task(
                status=TaskStatus.COMPLETED,
                context={
                    "_attempt_number": 2,
                    "_previous_failure_class": "CODE_ERROR",
                    "_prior_attempts_cost": 1.5,
                },
            ),
            make_task(
                status=TaskStatus.COMPLETED,
                context={
                    "_attempt_number": 3,
                    "_previous_failure_class": "CODE_ERROR",
                    "_prior_attempts_cost": 0.75,
                },
            ),
        ]
        result = compute_retry_analytics(
            failed_tasks=[],
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        assert result["retry_success_count"] == 2
        assert result["retry_recovery_cost_usd"] == round(1.5 + 0.75, 4)
        assert "CODE_ERROR" in result["retry_success_rate_by_class"]
        cls_entry = result["retry_success_rate_by_class"]["CODE_ERROR"]
        assert cls_entry["merged"] == 2
        assert cls_entry["failed"] == 0
        assert cls_entry["success_rate"] == 1.0
        # No failed retries → retry_wasted_rate denominator is 0+2=2, waste=0
        assert result["retry_wasted_rate"] == 0.0

    def test_retried_and_failed(self):
        """Failed tasks with _attempt_number > 1 contribute to retry_overhead_cost_usd
        and retry_waste_by_direction with top-5 truncation."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        # 6 directions — only top 5 by cost should appear
        failed = []
        dir_id_to_text = {}
        for i in range(6):
            did = f"dir-{i}"
            dir_id_to_text[did] = f"Direction {i}"
            duration_hours = i + 1  # dir-0=1h, dir-5=6h so dir-5 is most expensive
            t = make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2, "direction_id": did},
                vm_provisioned_at=now - timedelta(hours=duration_hours + 1),
                vm_stopped_at=now - timedelta(minutes=1),
                vm_spot=False,
            )
            failed.append(t)

        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=[],
            dir_id_to_text=dir_id_to_text,
            window_start=window_start,
        )
        assert result["retry_task_count"] == 6
        # Top-5 truncation: only 5 directions returned, sorted descending by cost
        assert len(result["retry_waste_by_direction"]) == 5
        costs = [e["retry_cost_usd"] for e in result["retry_waste_by_direction"]]
        assert costs == sorted(costs, reverse=True)
        # Overhead cost is positive
        assert result["retry_overhead_cost_usd"] > 0

    def test_retry_wasted_rate_denominator(self):
        """retry_wasted_rate = failed_retries / (failed_retries + succeeded_retries)."""
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        failed = [
            make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2},
            ),
        ]
        completed = [
            make_task(
                status=TaskStatus.COMPLETED,
                context={"_attempt_number": 2, "_previous_failure_class": "CODE_ERROR"},
            ),
            make_task(
                status=TaskStatus.COMPLETED,
                context={"_attempt_number": 2, "_previous_failure_class": "CODE_ERROR"},
            ),
        ]
        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=completed,
            dir_id_to_text={},
            window_start=window_start,
        )
        # 1 failed retry, 2 succeeded retries → wasted_rate = 1/3
        assert result["retry_task_count"] == 1
        assert result["retry_success_count"] == 2
        assert result["retry_wasted_rate"] == round(1 / 3, 4)


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_no_versions(db_session):
    """Empty DB returns zero-valued counters."""
    now = _utcnow()
    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=None,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 0
    assert result["release_infra_failure_total"] == 0
    assert result["audit_report_missing_count"] == 0
    assert result["audit_report_parseable_but_unscoreable_count"] == 0
    assert result["smoketest_report_missing_count"] == 0
    assert result["release_infra_failure_streak_max"] == 0


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_counts_by_error_category(db_session):
    """Rows with distinct infra_failure error codes increment the correct buckets."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    finalized_at = (now - timedelta(hours=1)).isoformat()

    error_cases = [
        "audit_report_missing",
        "audit_report_parseable_but_unscoreable",
        "smoketest_report_missing",
        "audit_task_failed_other",  # unknown bucket — counts in total only
    ]
    for i, error in enumerate(error_cases):
        v = ReleaseVersion(
            project_id=project.id,
            version_number=i + 1,
            phase="stabilize",
            last_qualification={
                "status": "infra_failure",
                "error": error,
                "consecutive_infra_errors": 1,
                "finalized_at": finalized_at,
            },
        )
        db_session.add(v)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=window_start,
    )
    assert result["qualifier_runs_total"] == 4
    assert result["release_infra_failure_total"] == 4
    assert result["audit_report_missing_count"] == 1
    assert result["audit_report_parseable_but_unscoreable_count"] == 1
    assert result["smoketest_report_missing_count"] == 1


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_streak_max(db_session):
    """release_infra_failure_streak_max reflects the highest consecutive_infra_errors."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    finalized_at = (now - timedelta(hours=1)).isoformat()

    for i, streak in enumerate([2, 4, 1]):
        v = ReleaseVersion(
            project_id=project.id,
            version_number=i + 1,
            phase="stabilize",
            last_qualification={
                "status": "infra_failure",
                "error": "audit_report_missing",
                "consecutive_infra_errors": streak,
                "finalized_at": finalized_at,
            },
        )
        db_session.add(v)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["release_infra_failure_streak_max"] == 4


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_excludes_outside_window(db_session):
    """Rows finalized before window_start are not counted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    old_finalized_at = (now - timedelta(hours=48)).isoformat()
    recent_finalized_at = (now - timedelta(hours=1)).isoformat()

    old_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "audit_report_missing",
            "consecutive_infra_errors": 3,
            "finalized_at": old_finalized_at,
        },
    )
    recent_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "smoketest_report_missing",
            "consecutive_infra_errors": 1,
            "finalized_at": recent_finalized_at,
        },
    )
    db_session.add(old_version)
    db_session.add(recent_version)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 1
    assert result["release_infra_failure_total"] == 1
    assert result["smoketest_report_missing_count"] == 1
    assert result["audit_report_missing_count"] == 0


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_qualifier_runs_total(db_session):
    """qualifier_runs_total counts all finalized quals, not just infra_failure ones."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    finalized_at = (now - timedelta(hours=1)).isoformat()

    # One infra_failure and one successful promotion
    infra_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        last_qualification={
            "status": "infra_failure",
            "error": "audit_report_missing",
            "consecutive_infra_errors": 1,
            "finalized_at": finalized_at,
        },
    )
    promoted_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="promote",
        last_qualification={
            "status": "promoted",
            "finalized_at": finalized_at,
        },
    )
    # Version with no qualification at all — should not count
    unqual_version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        last_qualification=None,
    )
    db_session.add(infra_version)
    db_session.add(promoted_version)
    db_session.add(unqual_version)
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 2
    assert result["release_infra_failure_total"] == 1


class TestDigestMetricsModuleSeams:
    """Seam guards for the digest_metrics decomposition.

    ``compute_remediation_funnel`` and ``compute_direction_progress`` were the
    two largest analytics in ``app.digest_metrics``. They now live in cohesive
    sibling modules (``app.digest_remediation`` and
    ``app.digest_direction_progress`` respectively) and are re-exported from
    ``app.digest_metrics`` so existing import paths (and ``mock.patch``
    targets) keep working. These tests fail loudly if either the canonical
    home moves or the re-export is dropped.
    """

    def test_remediation_funnel_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_remediation as digest_remediation

        # Canonical home defines it as a real function.
        assert hasattr(digest_remediation, "compute_remediation_funnel")
        # digest_metrics re-export resolves to the same object.
        assert (
            digest_metrics.compute_remediation_funnel
            is digest_remediation.compute_remediation_funnel
        )

    def test_remediation_and_patterns_still_in_remediation_module(self):
        import app.digest_metrics as digest_metrics
        import app.digest_remediation as digest_remediation

        assert (
            digest_metrics.compute_remediation_and_patterns
            is digest_remediation.compute_remediation_and_patterns
        )

    def test_direction_progress_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_direction_progress as digest_direction_progress

        assert hasattr(digest_direction_progress, "compute_direction_progress")
        assert (
            digest_metrics.compute_direction_progress
            is digest_direction_progress.compute_direction_progress
        )

    def test_extracted_modules_do_not_import_digest_metrics_at_load_time(self):
        """The new sibling modules must not import ``digest_metrics`` at module
        load time — that would create a circular import (digest_metrics imports
        them). Cross-module needs are satisfied via function-local imports."""
        import ast
        import os

        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        for module_file in ("digest_remediation.py", "digest_direction_progress.py"):
            path = os.path.normpath(os.path.join(app_dir, module_file))
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in tree.body:  # top-level statements only
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "app.digest_metrics", (
                        f"{module_file} imports app.digest_metrics at module "
                        "load time — this risks a circular import. Use a "
                        "function-local import instead."
                    )


# ── Dependency-health observability signals ────────────────────────────────


def test_parked_blocker_summary_counts_only_escalated_blocked():
    """Only BLOCKED tasks carrying ``_blocked_escalated`` are counted as parked;
    a still-rechecking blocker (no escalation flag) is excluded."""
    from app.digest_metrics import compute_parked_blocker_summary

    now = _utcnow()
    parked = make_task(
        status=TaskStatus.BLOCKED,
        title="parked-foundation",
        blocker_kind="upstream_dependency",
        blocked_reason="Waiting: foundation never landed",
        blocked_at=now - timedelta(hours=8),
        recheck_after=now + timedelta(days=3650),
        context={"_blocked_escalated": True, "direction_id": "d-1"},
    )
    still_rechecking = make_task(
        status=TaskStatus.BLOCKED,
        title="still-waiting",
        context={"_blocked_recheck_attempts": 2},
    )

    result = compute_parked_blocker_summary([parked, still_rechecking])

    assert result["total"] == 1
    assert len(result["tasks"]) == 1
    row = result["tasks"][0]
    assert row["task_id"] == str(parked.id)
    assert row["blocker_kind"] == "upstream_dependency"
    assert row["direction_id"] == "d-1"
    assert row["blocked_at"] is not None


def test_duplicate_foundation_signal_flags_cross_parent_overlap():
    """Two active write-tasks under DIFFERENT parents whose related-file sets
    strongly overlap are surfaced; a disjoint task is not."""
    from app.digest_metrics import compute_duplicate_foundation_signal

    p_a, p_b = uuid4(), uuid4()
    files = ["app/foundation.py", "app/db.py"]
    # Cross-parent, identical files → flagged.
    t1 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_a,
        payloads=[{"type": "create", "related_files": files}],
    )
    t2 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_b,
        payloads=[{"type": "create", "related_files": files}],
    )
    # Disjoint files → NOT flagged.
    t3 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create", "related_files": ["app/unrelated.py"]}],
    )

    result = compute_duplicate_foundation_signal([t1, t2, t3])

    assert result["total"] == 1
    cand = result["candidates"][0]
    assert set(cand["task_ids"]) == {str(t1.id), str(t2.id)}
    assert cand["overlap_ratio"] == 1.0
    assert cand["shared_files"] == ["app/db.py", "app/foundation.py"]


def test_duplicate_foundation_signal_skips_same_parent_siblings():
    """Same-parent siblings are intra-decomposition work gated by depends_on
    edges, not the silent cross-parent duplication this signal surfaces — so an
    overlapping same-parent pair is NOT flagged."""
    from app.digest_metrics import compute_duplicate_foundation_signal

    parent = uuid4()
    files = ["app/foundation.py", "app/db.py"]
    t1 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent,
        payloads=[{"type": "create", "related_files": files}],
    )
    t2 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent,
        payloads=[{"type": "fix", "related_files": files}],
    )

    result = compute_duplicate_foundation_signal([t1, t2])

    assert result["total"] == 0
    assert result["candidates"] == []


def test_duplicate_foundation_signal_ignores_non_write_tasks():
    """Research/plan (non code-change) active tasks never contribute to the
    duplicate-foundation signal even with overlapping declared files."""
    from app.digest_metrics import compute_duplicate_foundation_signal

    t1 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "research", "related_files": ["app/x.py"]}],
    )
    t2 = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "plan", "related_files": ["app/x.py"]}],
    )

    result = compute_duplicate_foundation_signal([t1, t2])

    assert result["total"] == 0
    assert result["candidates"] == []
