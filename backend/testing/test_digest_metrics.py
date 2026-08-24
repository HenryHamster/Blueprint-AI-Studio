"""Unit tests for digest metric helpers (app.digest_metrics / sr_review_quality_metrics)."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.models import (
    TaskStatus,
    QualificationVerdict,
    ReleaseVersion,
)
from app.digest_metrics import (
    _FAILURE_CLASS_LABELS,
    _SELF_REPAIR_MIN_ATTEMPTS,
    _compute_first_attempt_breakdowns,
    _compute_first_review_approval_breakdowns,
    _compute_non_code_outcomes,
    compute_churn_direction,
    compute_digest_cost_economics,
    compute_effort_model_efficiency,
    compute_provider_routing_share,
    compute_digest_trend_history,
    compute_failure_grouping,
    compute_format_compliance_metrics,
    compute_migration_health,
    compute_migration_slot_contention,
    compute_migration_slot_contention_summary,
    _MIGRATION_SLOT_HOLD_ALERT_SECONDS,
    compute_narrative_summary,
    compute_review_cap_exhaustion_summary,
    compute_outer_loop_convergence,
    compute_lane_waste_diagnostics,
    compute_dependency_health_signals,
    compute_recurring_error_clusters,
    compute_release_qualifier_infra_metrics,
    compute_retry_analytics,
    compute_scope_mismatch_summary,
    compute_task_type_cost_adherence,
    compute_trial_arm_accumulation,
    compute_trial_arm_cost_delta,
)
from app.failure_classification import ALL_FAILURE_CLASSES
from app.llm_cost_attribution import (
    LLM_TOKEN_PRICING,
    UNPRICED_FALLBACK_INPUT_RATE,
    UNPRICED_FALLBACK_OUTPUT_RATE,
)
from app.sr_review_quality_metrics import (
    compute_review_quality_metrics,
    compute_sr_prediction_accuracy,
)
from testing.factories import make_project, make_task, _utcnow


def _priced_llm_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    input_rate, output_rate = LLM_TOKEN_PRICING[(provider, model)]
    return input_tokens * input_rate + output_tokens * output_rate


@pytest.mark.asyncio
async def test_trial_arm_cost_uses_priced_denominator_above_coverage_threshold(
    db_session,
):
    """A small unpriced minority is visible without suppressing the cost signal."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provider, model = "deepseek", "deepseek-v4-flash"
    input_tokens = 1_000_000
    tasks = [
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_trial_arm": True},
            provider=provider if index else "unknown-provider",
            model=model if index else "unknown-model",
            api_call_summary={"total_input_tokens": input_tokens},
        )
        for index in range(20)
    ]
    tasks.extend(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_trial_arm": False},
            provider=provider,
            model=model,
            api_call_summary={"total_input_tokens": input_tokens},
        )
        for _ in range(20)
    )
    db_session.add_all(tasks)
    await db_session.commit()

    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )
    expected = round(input_tokens * LLM_TOKEN_PRICING[(provider, model)][0], 6)

    assert result["with_priced_task_count"] == 19
    assert result["with_priced_coverage_fraction"] == pytest.approx(0.95)
    assert result["with_avg_cost_usd"] == pytest.approx(expected)
    assert result["without_avg_cost_usd"] == pytest.approx(expected)
    assert result["cost_delta"] == pytest.approx(0.0)
    assert result["unpriced_model_pairs"] == [
        {"provider": "unknown-provider", "model": "unknown-model"}
    ]
@pytest.mark.asyncio
async def test_trial_arm_cost_suppresses_average_below_priced_coverage_threshold(
    db_session,
):
    """Low coverage suppresses the average but retains actionable missing pricing pairs."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    tasks = [
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_trial_arm": True},
            provider="unknown-provider",
            model="unknown-model",
            api_call_summary={"total_input_tokens": 1},
        )
        for _ in range(11)
    ]
    tasks.extend(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            context={"_trial_arm": True},
            provider="deepseek",
            model="deepseek-v4-flash",
            api_call_summary={"total_input_tokens": 1},
        )
        for _ in range(9)
    )
    db_session.add_all(tasks)
    await db_session.commit()
    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )

    assert result["with_priced_task_count"] == 9
    assert result["with_priced_coverage_fraction"] == pytest.approx(0.45)
    assert result["with_avg_cost_usd"] is None
    assert result["cost_delta"] is None
    assert result["unpriced_model_pairs"] == [
        {"provider": "unknown-provider", "model": "unknown-model"}
    ]
@pytest.mark.asyncio
async def test_trial_arm_cost_matches_existing_average_when_every_task_is_priced(
    db_session,
):
    """Fully priced arms retain the historical all-terminal-task average."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    provider, model = "deepseek", "deepseek-v4-flash"
    input_tokens = 1_000_000
    db_session.add_all(
        [
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
                provider=provider,
                model=model,
                api_call_summary={"total_input_tokens": input_tokens},
            ),
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                context={"_trial_arm": True},
            ),
        ]
    )
    await db_session.commit()
    result = await compute_trial_arm_cost_delta(
        db_session, project_id=project.id, context_key="_trial_arm"
    )
    expected = round(input_tokens * LLM_TOKEN_PRICING[(provider, model)][0] / 2, 6)

    assert result["with_priced_task_count"] == 2
    assert result["with_priced_coverage_fraction"] == 1.0
    assert result["with_avg_cost_usd"] == pytest.approx(expected)
    assert result["unpriced_model_pairs"] == []


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
        starting_count=0,
        evaluating_count=0,
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


def test_compute_narrative_summary_names_starting_and_evaluating_separately():
    assert (
        _build_narrative_summary(
            running_count=3, starting_count=1, evaluating_count=2
        )
        == "3 in progress, 1 starting, 2 evaluating."
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


def test_timeout_advice_reflects_dominant_subtype():
    """Timeout advice is driven by the actual dominant timeout subtype, not generic complexity."""
    from app.failure_classification import (
        AGENT_TIMEOUT,
    )

    # OOM-dominant timeouts → memory/footprint advice
    oom_tasks = [
        make_task(
            title=f"OOM Timeout {i}",
            status=TaskStatus.FAILED,
            context={"failure_category": AGENT_TIMEOUT, "timeout_subtype": "oom"},
            latest_status_message="OutOfMemory",
        )
        for i in range(3)
    ]
    oom_narrative = _build_narrative_summary(
        failed_count=3,
        failure_groups=[
            {
                "failure_class": AGENT_TIMEOUT,
                "count": 3,
                "label": "Agent Timeout",
                "tasks": [
                    {
                        "task_id": str(t.id),
                        "title": t.title,
                        "timeout_subtype": "oom",
                    }
                    for t in oom_tasks
                ],
            }
        ],
        failed_tasks=oom_tasks,
        error_log_map={},
    )
    assert "memory" in oom_narrative.lower() or "footprint" in oom_narrative.lower()
    assert "complexity" not in oom_narrative.lower()

    # Heartbeat-lost-dominant timeouts → hangs/liveness advice
    hb_tasks = [
        make_task(
            title=f"Heartbeat Timeout {i}",
            status=TaskStatus.FAILED,
            context={
                "failure_category": AGENT_TIMEOUT,
                "timeout_subtype": "heartbeat_lost",
            },
            latest_status_message="No heartbeat",
        )
        for i in range(3)
    ]
    hb_narrative = _build_narrative_summary(
        failed_count=3,
        failure_groups=[
            {
                "failure_class": AGENT_TIMEOUT,
                "count": 3,
                "label": "Agent Timeout",
                "tasks": [
                    {
                        "task_id": str(t.id),
                        "title": t.title,
                        "timeout_subtype": "heartbeat_lost",
                    }
                    for t in hb_tasks
                ],
            }
        ],
        failed_tasks=hb_tasks,
        error_log_map={},
    )
    assert "hangs" in hb_narrative.lower() or "liveness" in hb_narrative.lower()
    assert "complexity" not in hb_narrative.lower()

    # Execution-timeout-dominant timeouts → complexity/scope advice
    exec_tasks = [
        make_task(
            title=f"Execution Timeout {i}",
            status=TaskStatus.FAILED,
            context={
                "failure_category": AGENT_TIMEOUT,
                "timeout_subtype": "execution_timeout",
            },
            latest_status_message="Task timeout",
        )
        for i in range(3)
    ]
    exec_narrative = _build_narrative_summary(
        failed_count=3,
        failure_groups=[
            {
                "failure_class": AGENT_TIMEOUT,
                "count": 3,
                "label": "Agent Timeout",
                "tasks": [
                    {
                        "task_id": str(t.id),
                        "title": t.title,
                        "timeout_subtype": "execution_timeout",
                    }
                    for t in exec_tasks
                ],
            }
        ],
        failed_tasks=exec_tasks,
        error_log_map={},
    )
    assert "complexity" in exec_narrative.lower() or "scope" in exec_narrative.lower()

    # Cascade-cancellation-dominant timeouts → upstream advice
    cascade_tasks = [
        make_task(
            title=f"Cascade Cancel {i}",
            status=TaskStatus.FAILED,
            context={
                "failure_category": AGENT_TIMEOUT,
                "timeout_subtype": "cascade_cancellation",
            },
            latest_status_message="Cascade cancel",
        )
        for i in range(3)
    ]
    cascade_narrative = _build_narrative_summary(
        failed_count=3,
        failure_groups=[
            {
                "failure_class": AGENT_TIMEOUT,
                "count": 3,
                "label": "Agent Timeout",
                "tasks": [
                    {
                        "task_id": str(t.id),
                        "title": t.title,
                        "timeout_subtype": "cascade_cancellation",
                    }
                    for t in cascade_tasks
                ],
            }
        ],
        failed_tasks=cascade_tasks,
        error_log_map={},
    )
    assert (
        "upstream" in cascade_narrative.lower()
        or "dependencies" in cascade_narrative.lower()
    )


# ---------------------------------------------------------------------------
# Durable windowed AGENT_TIMEOUT-rate measure (compute_timeout_rate)
# ---------------------------------------------------------------------------


def _timeout_task(subtype: str, *, started: bool = True):
    """A terminal task with PERSISTED AGENT_TIMEOUT ground truth for the measure."""
    from app.failure_classification import AGENT_TIMEOUT

    return make_task(
        status=TaskStatus.FAILED,
        started_at=_utcnow() if started else None,
        context={"failure_category": AGENT_TIMEOUT, "timeout_subtype": subtype},
    )


def _nontimeout_terminal_task(failure_category: str | None = None):
    """A terminal task that reached execution but is NOT an AGENT_TIMEOUT."""
    ctx = {"failure_category": failure_category} if failure_category else {}
    return make_task(
        status=TaskStatus.COMPLETED if failure_category is None else TaskStatus.FAILED,
        started_at=_utcnow(),
        context=ctx,
    )


def test_compute_timeout_rate_empty_window_zero_rate():
    """Empty window: rate undefined (None) when nothing reached execution."""
    from app.digest_metrics import compute_timeout_rate

    block = compute_timeout_rate([])
    assert block["denominator"] == 0
    assert block["timeout_count"] == 0
    assert block["overall_rate"] is None
    assert block["dominant_subtype"] is None
    # All seven subtypes are present and zero-filled.
    assert set(block["by_subtype"]) == {
        "heartbeat_lost",
        "oom",
        "execution_timeout",
        "cascade_cancellation",
        "iteration_limit",
        "process_crash",
        "unknown",
    }
    assert all(v["count"] == 0 for v in block["by_subtype"].values())


def test_compute_timeout_rate_zero_timeouts_real_zero():
    """A window with execution but no timeouts: real 0.0 rate, not None."""
    from app.digest_metrics import compute_timeout_rate

    tasks = [_nontimeout_terminal_task() for _ in range(5)]
    tasks += [_nontimeout_terminal_task("CODE_ERROR") for _ in range(3)]
    block = compute_timeout_rate(tasks)
    assert block["denominator"] == 8
    assert block["timeout_count"] == 0
    assert block["overall_rate"] == 0.0
    assert block["dominant_subtype"] is None


def test_compute_timeout_rate_exact_rate_and_dominant_subtype():
    """Known persisted subtypes yield the EXACT expected rate and dominant subtype."""
    from app.digest_metrics import compute_timeout_rate

    # 4 oom + 1 heartbeat_lost timeouts among 10 execution-reached tasks.
    tasks = [_timeout_task("oom") for _ in range(4)]
    tasks.append(_timeout_task("heartbeat_lost"))
    tasks += [_nontimeout_terminal_task() for _ in range(5)]

    block = compute_timeout_rate(tasks)
    assert block["denominator"] == 10
    assert block["timeout_count"] == 5
    # 5 timeouts / 10 execution-reached = exactly 0.5.
    assert block["overall_rate"] == 0.5
    assert block["by_subtype"]["oom"]["count"] == 4
    assert block["by_subtype"]["oom"]["rate"] == 0.4
    assert block["by_subtype"]["heartbeat_lost"]["rate"] == 0.1
    # Per-subtype rates sum to the overall rate (same denominator).
    assert round(sum(v["rate"] for v in block["by_subtype"].values()), 4) == 0.5
    assert block["dominant_subtype"] == "oom"
    assert block["dominant_share"] == 0.8  # 4/5


def test_compute_timeout_rate_not_gameable_by_raw_volume():
    """Raw volume alone does not move the rate when the denominator scales with it."""
    from app.digest_metrics import compute_timeout_rate

    # 2 timeouts among 10 execution-reached -> 0.2.
    small = [_timeout_task("oom") for _ in range(2)] + [
        _nontimeout_terminal_task() for _ in range(8)
    ]
    small_rate = compute_timeout_rate(small)["overall_rate"]

    # Scale BOTH numerator and denominator by 5x: 10 timeouts among 50 -> still 0.2.
    big = [_timeout_task("oom") for _ in range(10)] + [
        _nontimeout_terminal_task() for _ in range(40)
    ]
    big_block = compute_timeout_rate(big)
    assert small_rate == 0.2
    assert big_block["overall_rate"] == 0.2  # raw volume 5x but rate unchanged
    assert big_block["timeout_count"] == 10
    assert big_block["denominator"] == 50


def test_compute_timeout_rate_denominator_excludes_never_started():
    """Never-started non-timeouts are excluded, but never-started timeouts are included."""
    from app.digest_metrics import compute_timeout_rate

    tasks = [_timeout_task("oom") for _ in range(2)]
    tasks += [_nontimeout_terminal_task() for _ in range(2)]
    # Two terminal tasks that never started — the timeout is included in denominator
    # (it could have timed out, and did), but the non-timeout is excluded (it could
    # not have timed out).
    tasks += [_timeout_task("oom", started=False)]
    tasks += [make_task(status=TaskStatus.CANCELLED, started_at=None, context={})]
    block = compute_timeout_rate(tasks)
    # Denominator = 5: the 2 started timeouts + 2 started non-timeouts +
    # 1 unstarted timeout. The unstarted non-timeout is not included.
    assert block["denominator"] == 5
    assert block["timeout_count"] == 3
    assert block["by_subtype"]["oom"]["count"] == 3


def test_compute_timeout_rate_reads_persisted_not_derived():
    """The measure ignores log/status text and reads only persisted ground truth."""
    from app.digest_metrics import compute_timeout_rate
    from app.failure_classification import AGENT_TIMEOUT

    # Status/log text screams OOM, but persisted ground truth says heartbeat_lost.
    # The measure MUST honour the persisted subtype, not re-derive from the text.
    t = make_task(
        status=TaskStatus.FAILED,
        started_at=_utcnow(),
        latest_status_message="OutOfMemory killed the worker, oom oom oom",
        context={
            "failure_category": AGENT_TIMEOUT,
            "timeout_subtype": "heartbeat_lost",
        },
    )
    block = compute_timeout_rate([t])
    assert block["by_subtype"]["heartbeat_lost"]["count"] == 1
    assert block["by_subtype"]["oom"]["count"] == 0

    # A task whose status text looks like a timeout but has NO persisted
    # AGENT_TIMEOUT category is NOT counted as a timeout.
    not_persisted = make_task(
        status=TaskStatus.FAILED,
        started_at=_utcnow(),
        latest_status_message="execution timeout after 1h",
        context={"failure_category": "CODE_ERROR"},
    )
    block2 = compute_timeout_rate([not_persisted])
    assert block2["timeout_count"] == 0


def test_compute_timeout_rate_counts_unstarted_timeout_in_denominator():
    """Unstarted AGENT_TIMEOUT tasks are included in the denominator.

    A never-started timeout is still a task that could have timed out (it did,
    just before dispatching), so it should count in the denominator alongside
    started tasks. This ensures numerator and denominator are consistent and the
    rate is not inflated.
    """
    from app.digest_metrics import compute_timeout_rate

    # 2 started timeouts + 2 non-timeout started tasks
    tasks = [_timeout_task("oom") for _ in range(2)]
    tasks += [_nontimeout_terminal_task() for _ in range(2)]
    # 1 unstarted timeout + 1 unstarted non-timeout
    tasks.append(_timeout_task("oom", started=False))
    tasks.append(make_task(status=TaskStatus.CANCELLED, started_at=None, context={}))

    block = compute_timeout_rate(tasks)
    # Denominator = 5: the 2 started timeouts + 2 started non-timeouts +
    # 1 unstarted timeout. The unstarted non-timeout is NOT in denominator.
    assert block["denominator"] == 5
    # Numerator = 3: all three timeouts (2 started + 1 unstarted).
    assert block["timeout_count"] == 3
    assert block["by_subtype"]["oom"]["count"] == 3
    # Rate = 3/5 = 0.6
    assert block["overall_rate"] == 0.6
    assert block["by_subtype"]["oom"]["rate"] == 0.6


def test_confident_dominant_timeout_subtype_gate():
    """Dominant subtype is gated: needs >=2 and >=60%, and excludes 'unknown'."""
    from app.digest_metrics import confident_dominant_timeout_subtype

    # 3 oom of 4 timeouts -> 75% -> confident "oom".
    tasks = [_timeout_task("oom") for _ in range(3)] + [
        _timeout_task("execution_timeout")
    ]
    assert confident_dominant_timeout_subtype(tasks) == "oom"

    # 1 oom of 1 -> count < 2 -> not confident.
    assert confident_dominant_timeout_subtype([_timeout_task("oom")]) is None

    # 2-2 split -> 50% -> not confident.
    split = [_timeout_task("oom") for _ in range(2)] + [
        _timeout_task("heartbeat_lost") for _ in range(2)
    ]
    assert confident_dominant_timeout_subtype(split) is None

    # 3 unknown of 3 -> dominant but 'unknown' is suppressed.
    unknowns = [_timeout_task("unknown") for _ in range(3)]
    assert confident_dominant_timeout_subtype(unknowns) is None


def test_format_timeout_rate_drift_sentence():
    """The before/after sentence renders fractions as whole-percent and direction."""
    from app.digest_metrics import _format_timeout_rate_drift

    down = _format_timeout_rate_drift({"current": 0.08, "baseline": 0.12})
    assert down == "Agent-timeout rate 8%, down from a baseline of 12%."
    up = _format_timeout_rate_drift({"current": 0.20, "baseline": 0.10})
    assert up == "Agent-timeout rate 20%, up from a baseline of 10%."
    same = _format_timeout_rate_drift({"current": 0.10, "baseline": 0.10})
    assert same == "Agent-timeout rate 10%, unchanged from baseline."
    # Missing baseline -> no sentence (nothing durable to report yet).
    assert _format_timeout_rate_drift({"current": 0.08, "baseline": None}) is None
    assert _format_timeout_rate_drift(None) is None


def test_narrative_timeout_before_after_sentence_appended():
    """When timeouts dominate AND a drift block is supplied, the narrative reports drift."""
    oom_tasks = [_timeout_task("oom") for _ in range(3)]
    from app.failure_classification import AGENT_TIMEOUT

    narrative = _build_narrative_summary(
        failed_count=3,
        failure_groups=[
            {"failure_class": AGENT_TIMEOUT, "count": 3, "label": "Agent Timeout"}
        ],
        failed_tasks=oom_tasks,
        timeout_rate_drift={"current": 0.05, "baseline": 0.12},
    )
    # Persisted-ground-truth dominant subtype still routes the advice (oom).
    assert "memory" in narrative.lower() or "footprint" in narrative.lower()
    # Before/after sentence appended.
    assert "Agent-timeout rate 5%, down from a baseline of 12%." in narrative


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


def test_compute_migration_health_counts_migration_errors_only_for_migrate_tasks():
    migrate_completed = make_task(
        status=TaskStatus.COMPLETED, payloads=[{"type": "migrate"}]
    )
    migrate_failed = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "migrate"}],
        context={"failure_class": "MIGRATION_ERROR"},
    )
    non_migrate_completed = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context={"failure_class": "MIGRATION_ERROR"},
    )
    non_migrate_failed = make_task(
        status=TaskStatus.FAILED,
        payloads=[{"type": "create"}],
        context={"failure_class": "MIGRATION_ERROR"},
    )

    result = compute_migration_health(
        [migrate_completed, non_migrate_completed],
        [migrate_failed, non_migrate_failed],
        error_log_map={},
    )

    assert result["migration_tasks_in_window"] == 2
    assert result["migration_tasks_succeeded"] == 1
    assert result["migration_tasks_failed"] == 1
    assert result["migration_errors_in_window"] == 1


def test_compute_migration_health_reports_migration_slot_contention():
    now = _utcnow()
    project_id = uuid4()
    holder = make_task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        title="Author migration",
        payloads=[{"type": "migrate"}],
        started_at=now - timedelta(hours=3, minutes=15),
    )
    oldest_waiter = make_task(
        project_id=project_id,
        status=TaskStatus.BLOCKED,
        title="Blocked migration waiter",
        payloads=[
            {
                "type": "create",
                "related_files": ["backend/app/models.py"],
            }
        ],
        context={"related_files": ["backend/app/models.py"]},
        created_at=now - timedelta(hours=1, minutes=45),
        updated_at=now - timedelta(hours=1, minutes=30),
    )
    newer_waiter = make_task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        title="Younger migration waiter",
        payloads=[{"type": "migrate"}],
        created_at=now - timedelta(minutes=50),
        updated_at=now - timedelta(minutes=20),
    )

    result = compute_migration_health(
        [],
        [],
        error_log_map={},
        active_tasks=[holder, oldest_waiter, newer_waiter],
    )

    contention = result["migration_slot_contention"]
    assert contention["status"] == "elevated"
    assert contention["severity"] == "critical"
    assert contention["deferred_count"] == 2
    assert contention["holder"]["task_id"] == str(holder.id)
    assert contention["holder"]["hold_seconds"] >= 3 * 3600
    assert contention["longest_wait"]["task_id"] == str(oldest_waiter.id)
    assert contention["longest_wait"]["wait_seconds"] >= 90 * 60


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
        {"incidence_counts": {"churn": c}, "churn_total": c} for c in (2, 8, 9, 10, 3)
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


def test_compute_narrative_summary_includes_spawn_gate_before_after_sentence():
    """Pipeline narrative compares challenged vs unchallenged spawn outcomes."""
    narrative = _build_narrative_summary(
        failed_count=1,
        failure_groups=[],
        failure_taxonomy_distribution={
            "categories": {},
            "total_failures": 0,
            "dominant_category": None,
        },
        direction_progress_list=[
            {
                "direction_text": "Spawn gate direction",
                "prs_merged": 0,
                "tasks_created": 1,
                "research_spawn_gate": {
                    "challenged_spawned": 2,
                    "challenged_merged": 1,
                    "challenged_merge_rate": 0.5,
                    "unchallenged_spawned": 3,
                    "unchallenged_merged": 1,
                    "unchallenged_merge_rate": 0.333,
                    "refuted_count": 2,
                },
            }
        ],
        failed_tasks=[],
    )
    assert "Before/after, research spawn gate:" in narrative
    assert "challenged-and-survived spawns merged at 50.0% versus 33.3%" in narrative
    assert "2 refuted findings were prevented from spawning." in narrative


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
    # Default compute-job spend is 0.0, so total == VM == 0.0 and split keys exist.
    assert result["daily_vm_cost_usd"] == 0.0
    assert result["daily_compute_cost_usd"] == 0.0
    assert result["daily_cost_ceiling_usd"] == 12.5
    assert result["overall_product_ratio"] is None
    assert result["median_cost_per_merged_pr"] is None
    assert result["cost_per_merge_by_type"] == {}
    assert result["median_cpm_by_type"] is None
    assert result["pipeline_acceptance_rate"] is None
    assert result["pipeline_conversion_rate"] is None
    # Retired: pipeline_autonomous/assisted_prs_merged + pipeline_operator_action_count
    # (consumer-less digest duplicates of per-direction outcome fields).
    assert "pipeline_autonomous_prs_merged" not in result
    assert "pipeline_assisted_prs_merged" not in result
    assert "pipeline_operator_action_count" not in result
    assert result["overall_type_distribution"] == {}
    assert result["pipeline_filter_funnel"] == {}
    assert "pipeline_generalized_successes" not in result
    assert "pipeline_generalized_health_rate" not in result
    assert "pipeline_outcome_kind_breakdown" not in result


def test_compute_digest_cost_economics_includes_compute_job_spend():
    """compute_job_daily_cost_usd folds into daily_cost_usd as total spend."""
    from app.routers.admin_analytics import ON_DEMAND_RATE

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    # A task that stopped earlier today (within today_start..now).
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    od_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix"}],
        vm_spot=False,
        vm_provisioned_at=max(window_start, today_start) + timedelta(seconds=0),
        vm_stopped_at=max(window_start, today_start) + timedelta(seconds=3600),
    )
    result = compute_digest_cost_economics(
        all_tasks=[od_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
        compute_job_daily_cost_usd=2.5,
    )
    expected_vm = round(ON_DEMAND_RATE * 3600, 2)
    assert result["daily_vm_cost_usd"] == expected_vm
    assert result["daily_compute_cost_usd"] == 2.5
    assert result["daily_cost_usd"] == round(expected_vm + 2.5, 2)


def test_compute_digest_cost_economics_includes_running_vm():
    """An in-flight task (vm_stopped_at=None, vm_provisioned_at today) contributes
    to daily_vm_cost_usd estimated to now rather than being treated as zero."""
    now = _utcnow()
    # Task provisioned 1 hour ago, still running (no vm_stopped_at)
    running_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        vm_spot=False,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=None,
    )
    result = compute_digest_cost_economics(
        all_tasks=[running_task],
        direction_progress_list=[],
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # 1 hour on-demand e2-standard-2 estimated to now ≈ $0.067
    assert result["daily_vm_cost_usd"] > 0.0, (
        "In-flight VM with vm_stopped_at=None should contribute to daily_vm_cost_usd"
    )
    assert result["daily_vm_cost_usd"] == pytest.approx(0.067, abs=0.005)


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


def test_cost_per_merge_excludes_typeless_bucket_from_median():
    """Typeless bucket ("unknown" type) is excluded from median that drives retirement threshold.

    When tasks have no declared type in first_payload, they default to "unknown".
    The median_cpm_by_type should only include real task types, not the phantom
    "unknown" bucket, so that retirement thresholds are computed from meaningful
    type cohorts rather than being skewed by untyped tasks.
    """
    now = _utcnow()
    window_start = now - timedelta(hours=24)

    # Create task with "create" type, 1 hour, merged
    create_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )

    # Create task with "refactor" type, 2 hours, merged
    refactor_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=2),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )

    # Create task with NO type (defaults to "unknown"), 100 hours, merged
    # With three task types having costs [1h, 2h, 100h]:
    # - Median including unknown: statistics.median([1h, 2h, 100h]) = 2h
    # - Median excluding unknown: statistics.median([1h, 2h]) = 1.5h
    # These are clearly different, so the test will catch the bug
    typeless_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{}],  # No type field
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=100),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )

    result = compute_digest_cost_economics(
        all_tasks=[create_task, refactor_task, typeless_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )

    by_type = result["cost_per_merge_by_type"]

    # All three types should be present in the result
    assert "create" in by_type
    assert "refactor" in by_type
    assert "unknown" in by_type

    # All should have merged counts and costs
    assert by_type["create"]["merged_count"] == 1
    assert by_type["refactor"]["merged_count"] == 1
    assert by_type["unknown"]["merged_count"] == 1

    create_cpm = by_type["create"]["cost_per_merge"]
    refactor_cpm = by_type["refactor"]["cost_per_merge"]
    unknown_cpm = by_type["unknown"]["cost_per_merge"]

    # Verify costs are as expected (roughly proportional to runtime)
    assert create_cpm is not None
    assert refactor_cpm is not None
    assert unknown_cpm is not None
    assert refactor_cpm > create_cpm  # 2 hours > 1 hour
    assert unknown_cpm > refactor_cpm * 10  # 100 hours >> 2 hours

    # The critical assertion: median should be based on create and refactor only
    # (the "real" types), not include the expensive "unknown" bucket.
    # Median with all three = 2h * rate (the middle value of [1h, 2h, 100h])
    # Median without unknown = 1.5h * rate (average of [1h, 2h])
    # These are distinctly different.
    median_cpm = result["median_cpm_by_type"]
    expected_median_without_unknown = (create_cpm + refactor_cpm) / 2
    expected_median_with_unknown = refactor_cpm  # Median of sorted [create, refactor, unknown]

    # The median should be close to the average of create and refactor
    # (excluding the expensive unknown bucket), NOT including unknown
    assert median_cpm is not None
    assert abs(median_cpm - expected_median_without_unknown) < expected_median_without_unknown * 0.01
    # Make sure it's NOT close to the median that would include unknown
    assert abs(median_cpm - expected_median_with_unknown) > expected_median_with_unknown * 0.1


def test_pipeline_conversion_rate_uses_pr_eligible_denominator():
    """Headline pipeline_conversion_rate divides merges by PR-eligible tasks only.

    direction_progress_list entries carry both tasks_created (all task rows) and
    pr_eligible_tasks_created (excludes research/audit/memo/investigate). The
    pipeline conversion rate must use the eligible count so non-PR work does not
    dilute the rate: 3 merged / 5 eligible = 0.6, not 3 / 12 created.
    """
    now = _utcnow()
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=[
            {
                "estimated_cost_usd": 0.0,
                "prs_merged": 2,
                "tasks_created": 7,
                "pr_eligible_tasks_created": 3,
            },
            {
                "estimated_cost_usd": 0.0,
                "prs_merged": 1,
                "tasks_created": 5,
                "pr_eligible_tasks_created": 2,
            },
        ],
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # 3 merged / 5 PR-eligible tasks = 0.6 (NOT 3 / 12 created = 0.25)
    assert result["pipeline_conversion_rate"] == 0.6


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
            "pr_eligible_tasks_created": 8,
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
            "pr_eligible_tasks_created": 4,
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
    # merged=4, rejected=2 → 4/6 = 0.667; PR-eligible=8+4=12 → 4/12 = 0.333.
    assert result["pipeline_acceptance_rate"] == 0.667
    assert result["pipeline_conversion_rate"] == 0.333
    # Retired: pipeline_autonomous/assisted_prs_merged + pipeline_operator_action_count
    # (consumer-less digest duplicates of per-direction outcome fields).
    assert "pipeline_autonomous_prs_merged" not in result
    assert "pipeline_assisted_prs_merged" not in result
    assert "pipeline_operator_action_count" not in result
    assert result["overall_type_distribution"] == {"create": 7, "fix": 1}
    assert result["pipeline_filter_funnel"] == {"a": 3, "b": 3}
    assert "pipeline_generalized_successes" not in result
    assert "pipeline_generalized_health_rate" not in result
    assert "pipeline_outcome_kind_breakdown" not in result


def test_compute_digest_cost_economics_pipeline_uses_raw_acceptance_rate():
    """Pipeline acceptance/conversion rates sum RAW merge counts (operator-accepted),
    matching the per-direction acceptance_rate convention in digest_direction_progress
    and direction_metrics. Unverified-degraded merges are counted like any other."""
    now = _utcnow()
    dirs = [
        {
            # Genuine direction: every merge is verified.
            "estimated_cost_usd": 0.0,
            "prs_merged": 3,
            "verified_prs_merged": 3,
            "prs_rejected": 1,
            "tasks_created": 10,
            "pr_eligible_tasks_created": 10,
        },
        {
            # Degraded direction: 2 merges, none verified (no-CI fall-open).
            "estimated_cost_usd": 0.0,
            "prs_merged": 2,
            "verified_prs_merged": 0,
            "prs_rejected": 1,
            "tasks_created": 5,
            "pr_eligible_tasks_created": 5,
        },
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # Raw merged = 3 + 2 = 5; rejected = 1 + 1 = 2.
    # acceptance = 5 / (5 + 2) = 5/7 ≈ 0.714; conversion = 5 / 15 ≈ 0.333.
    assert result["pipeline_acceptance_rate"] == round(5 / 7, 3)
    assert result["pipeline_conversion_rate"] == round(5 / 15, 3)


def test_compute_digest_cost_economics_pipeline_raw_with_verified_present():
    """Pipeline acceptance rates use raw prs_merged regardless of whether
    verified_prs_merged is present. The verified field is ignored for
    acceptance_rate (though still used separately for verified-specific metrics)."""
    now = _utcnow()
    dirs = [
        {
            # Raw and verified differ; raw is used.
            "estimated_cost_usd": 0.0,
            "prs_merged": 4,
            "verified_prs_merged": 2,
            "prs_rejected": 1,
            "tasks_created": 8,
            "pr_eligible_tasks_created": 8,
        },
        {
            "estimated_cost_usd": 0.0,
            "prs_merged": 3,
            "verified_prs_merged": 1,
            "prs_rejected": 0,
            "tasks_created": 4,
            "pr_eligible_tasks_created": 4,
        },
    ]
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=dirs,
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # merged = 4 + 3 = 7 (RAW, not verified 2 + 1 = 3); rejected = 1; created = 12.
    assert result["pipeline_acceptance_rate"] == round(7 / 8, 3)
    assert result["pipeline_conversion_rate"] == round(7 / 12, 3)


def test_cost_economics_includes_llm_dollar_companions():
    """LLM-dollar companion fields are present, priced from token data, and the
    existing VM-only fields are unchanged for token-free task sets."""
    from app.routers.admin_analytics import SPOT_RATE

    now = _utcnow()
    window_start = now - timedelta(hours=24)

    # Token-free task: VM cost only, so LLM companions must still be present with 0.
    no_token_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        context={"pr_merged": True},
    )
    result_no_tokens = compute_digest_cost_economics(
        all_tasks=[no_token_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    # New companion keys exist on token-free input.
    assert "total_llm_cost_usd" in result_no_tokens
    assert "llm_share_of_combined_cost" in result_no_tokens
    assert "median_llm_cost_per_merged_pr" in result_no_tokens
    # No tokens → LLM cost is 0, share depends on VM spend.
    assert result_no_tokens["total_llm_cost_usd"] == 0.0
    # VM-only fields unchanged.
    expected_vm_cost = round(SPOT_RATE * 1800, 2)
    assert result_no_tokens["total_spot_cost_usd"] == pytest.approx(expected_vm_cost)

    provider, model = "anthropic", "claude-opus-4-8"
    # Task with priced tokens; expected costs derive from the shared pricing table.
    merged_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        provider=provider,
        model=model,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 1000,
            "total_output_tokens": 2000,
            "llm_call_count": 5,
        },
    )
    unmerged_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        vm_spot=True,
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now - timedelta(minutes=30),
        provider=provider,
        model=model,
        context={},
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 1000,
            "llm_call_count": 3,
        },
    )
    result = compute_digest_cost_economics(
        all_tasks=[merged_task, unmerged_task],
        direction_progress_list=[],
        window_start=window_start,
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    expected_merged_llm = _priced_llm_cost(provider, model, 1000, 2000)
    expected_unmerged_llm = _priced_llm_cost(provider, model, 500, 1000)
    expected_total_llm = expected_merged_llm + expected_unmerged_llm
    assert result["total_llm_cost_usd"] == pytest.approx(expected_total_llm, rel=1e-4)
    # LLM share = LLM / (VM + LLM); VM is spot * 2 tasks * 1800s each.
    vm_each = SPOT_RATE * 1800
    vm_total = 2 * vm_each
    expected_share = expected_total_llm / (vm_total + expected_total_llm)
    assert result["llm_share_of_combined_cost"] == pytest.approx(
        expected_share, rel=1e-3
    )
    # median_llm_cost_per_merged_pr counts only merged tasks (1 task).
    assert result["median_llm_cost_per_merged_pr"] == pytest.approx(
        expected_merged_llm, rel=1e-4
    )
    # Existing VM-only fields unchanged.
    assert result["total_spot_cost_usd"] == pytest.approx(round(vm_total, 2), rel=1e-4)


def test_cost_economics_llm_companions_empty_input():
    """Empty task list returns zeroed LLM companions alongside zero VM fields."""
    now = _utcnow()
    result = compute_digest_cost_economics(
        all_tasks=[],
        direction_progress_list=[],
        window_start=now - timedelta(hours=24),
        now=now,
        daily_cost_ceiling_usd=0.0,
    )
    assert result["total_llm_cost_usd"] == 0.0
    assert result["llm_share_of_combined_cost"] is None
    assert result["median_llm_cost_per_merged_pr"] is None


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


def test_compute_review_quality_metrics_includes_later_iteration_only_confirmations():
    """A task whose reviewer-confirmed categories appear only on a LATER
    iteration (``_all_iterations_rejection_categories`` set, no
    ``_first_attempt_rejection_categories``) must be sampled by
    compute_review_quality_metrics and its later-confirmed category scored as a
    hit — not dropped from the sample, not counted as a false alarm."""
    tasks = [
        make_task(
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"correctness": {"passed": False}}
                },
                # Confirmed on a later iteration only; first review approved.
                "_all_iterations_rejection_categories": {"correctness": 1},
            },
        )
        for _ in range(5)
    ]
    result = compute_review_quality_metrics(tasks)
    pred = result["self_review_prediction_accuracy"]
    assert pred is not None
    # All five later-iteration-only tasks are in the sample.
    assert pred["sample_size"] == 5
    # SR flagged correctness AND reviewer confirmed it (later) => a hit, not a
    # false alarm.
    assert pred["prediction_hit_rate"] == 1.0
    assert pred["false_alarm_rate"] == 0.0


def test_compute_review_quality_metrics_empty_returns_nones():
    """Empty input → every metric is None / empty-ish per sample-size guards."""
    result = compute_review_quality_metrics([])
    assert result["self_review_effectiveness"] is None
    assert result["self_review_prediction_accuracy"] is None
    assert result["review_accuracy"] is None


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
        context={"research_surviving_findings": 1},
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


def test_compute_non_code_outcomes_includes_window_straddling_terminal_task():
    """Task created before window but completed within window is included.

    A task created before window_start but whose terminal status transition
    (completion/failure/cancellation) occurs within the window should be counted
    in non-code outcomes. This ensures we measure the actual delivery of outcomes
    within a time period, not the spawn time.
    """
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    # Created well before the window
    created_before_window = window_start - timedelta(hours=12)
    # But completed within the window
    completed_in_window = window_start + timedelta(hours=1)

    task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        plan_output=[{"title": "Investigate", "actionability_grade": "HIGH"}],
        # Raw research output is resumable handler state, not an outcome. Pin
        # the durable post-ingestion survivor signal so this test isolates the
        # completion-window behavior it is named for.
        context={"research_surviving_findings": 1},
        created_at=created_before_window,
        status_log=[{"s": "COMPLETED", "t": completed_in_window.isoformat()}],
    )

    result = _compute_non_code_outcomes([task], window_start)
    # Task should be included because it completed within the window.
    assert result["total"] == 1
    assert result["successes"] == 1
    assert result["neutrals"] == 0
    assert result["failures"] == 0


def test_outer_loop_convergence_non_code_outcomes_includes_straddling_task():
    """compute_outer_loop_convergence counts non-code tasks that straddle the window.

    A task created before window_start but completing within the window must appear
    in non_code_outcomes. This validates that the DB query layer must supply tasks
    with a wider lookback (created_at < window_start) so _compute_non_code_outcomes
    (which correctly windows on completion time) can count them.
    """
    now = _utcnow()
    window_start = now - timedelta(hours=24)
    # Created well before the window but completed within it
    created_before_window = window_start - timedelta(hours=12)
    completed_in_window = window_start + timedelta(hours=2)

    straddling_task = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        plan_output=[{"title": "Finding", "actionability_grade": "HIGH"}],
        # Raw research output is resumable handler state, not an outcome. Pin
        # the durable post-ingestion survivor signal so this test isolates the
        # completion-window behavior it is named for.
        context={"research_surviving_findings": 1},
        created_at=created_before_window,
        status_log=[{"s": "COMPLETED", "t": completed_in_window.isoformat()}],
    )

    result = compute_outer_loop_convergence(
        all_tasks=[straddling_task],
        escalation_alerts=[],
        window_start=window_start,
    )

    non_code = result.get("non_code_outcomes", {})
    assert non_code.get("total") == 1, (
        "Straddling non-code task (created before window, completed within) must "
        "appear in non_code_outcomes; DB layer must use a wider lookback_cutoff"
    )
    assert non_code.get("successes") == 1


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

    def test_avg_retries_before_failure_correct(self):
        """avg_retries_before_failure counts actual retries (attempt_number - 1), not attempt ordinal.

        _attempt_number is 1-indexed: 1=first attempt, 2=second attempt (1 retry), 3=third (2 retries).
        The average should reflect the number of retries, not the attempt number.
        """
        now = _utcnow()
        window_start = now - timedelta(hours=24)
        failed = [
            # _attempt_number=2 → 1 retry (second attempt)
            make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2},
            ),
            # _attempt_number=3 → 2 retries (third attempt)
            make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 3},
            ),
            # _attempt_number=2 → 1 retry
            make_task(
                status=TaskStatus.FAILED,
                context={"_attempt_number": 2},
            ),
        ]
        result = compute_retry_analytics(
            failed_tasks=failed,
            completed_tasks=[],
            dir_id_to_text={},
            window_start=window_start,
        )
        # 3 retried failed tasks: (1 + 2 + 1) retries / 3 tasks = 4 / 3 ≈ 1.33
        assert result["retry_task_count"] == 3
        assert result["avg_retries_before_failure"] == round(4 / 3, 2)


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
    assert result["release_infra_failure_repeat_signature_total"] == 0
    assert result["release_infra_failure_repeat_different_candidate_total"] == 0


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_counts_by_error_category(db_session):
    """Verdict rows with distinct infra_failure error codes increment correct buckets."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, phase="stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    window_start = now - timedelta(hours=24)
    finalized_at = now - timedelta(hours=1)

    error_cases = [
        "audit_report_missing",
        "audit_report_parseable_but_unscoreable",
        "smoketest_report_missing",
        "audit_task_failed_other",  # unknown bucket — counts in total only
    ]
    for error in error_cases:
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                terminal_status="infra_failure",
                finalized_at=finalized_at,
                sub_scores={"infra_failure": {"error": error, "consecutive_infra_errors": 1}},
            )
        )
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
async def test_release_qualifier_infra_metrics_counts_repeat_signatures(db_session):
    """Repeated infra signatures are surfaced as digest counters."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, phase="stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    finalized_at = now - timedelta(hours=1)
    infra_evidences = [
        {"error": "audit_infra_respawn_cap_exceeded", "consecutive_infra_errors": 1, "same_error_as_prior": True, "same_error_different_candidate": True},
        {"error": "audit_infra_respawn_cap_exceeded", "consecutive_infra_errors": 2, "same_error_as_prior": True, "same_error_different_candidate": False},
        {"error": "audit_infra_respawn_cap_exceeded", "consecutive_infra_errors": 3, "same_error_as_prior": False, "same_error_different_candidate": False},
    ]
    for evidence in infra_evidences:
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                terminal_status="infra_failure",
                finalized_at=finalized_at,
                sub_scores={"infra_failure": evidence},
            )
        )
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["release_infra_failure_total"] == 3
    assert result["release_infra_failure_repeat_signature_total"] == 2
    assert result["release_infra_failure_repeat_different_candidate_total"] == 1


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_streak_max(db_session):
    """release_infra_failure_streak_max reflects the highest consecutive_infra_errors."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, phase="stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    finalized_at = now - timedelta(hours=1)

    for streak in [2, 4, 1]:
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                terminal_status="infra_failure",
                finalized_at=finalized_at,
                sub_scores={"infra_failure": {"error": "audit_report_missing", "consecutive_infra_errors": streak}},
            )
        )
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["release_infra_failure_streak_max"] == 4


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_excludes_outside_window(db_session):
    """Verdict rows finalized before window_start are not counted."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, phase="stabilize")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    old_finalized_at = now - timedelta(hours=48)
    recent_finalized_at = now - timedelta(hours=1)

    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="infra_failure",
            finalized_at=old_finalized_at,
            sub_scores={"infra_failure": {"error": "audit_report_missing", "consecutive_infra_errors": 3}},
        )
    )
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="infra_failure",
            finalized_at=recent_finalized_at,
            sub_scores={"infra_failure": {"error": "smoketest_report_missing", "consecutive_infra_errors": 1}},
        )
    )
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
    """qualifier_runs_total counts all finalized verdict rows, not just infra_failure ones."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, phase="promote")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    now = _utcnow()
    finalized_at = now - timedelta(hours=1)

    # One infra_failure verdict
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="infra_failure",
            finalized_at=finalized_at,
            sub_scores={"infra_failure": {"error": "audit_report_missing", "consecutive_infra_errors": 1}},
        )
    )
    # One promoted verdict — counts in total but not infra
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="promoted",
            finalized_at=finalized_at,
            sub_scores=None,
        )
    )
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    assert result["qualifier_runs_total"] == 2
    assert result["release_infra_failure_total"] == 1


@pytest.mark.asyncio
async def test_release_qualifier_infra_metrics_count_historical_verdict_rows_after_requalification(
    db_session,
):
    """Infra attempts preserved as verdict rows are counted even after a later
    successful requalification overwrites last_qualification.

    This is the core regression test for the bug: ReleaseVersion.last_qualification
    is mutable and holds only the latest verdict. QualificationVerdict rows are
    immutable; each run appends one. After a successful re-qualification the infra
    rows must still be visible to the infra-metrics counter.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    finalized_at = now - timedelta(hours=2)

    # Single version that had two infra failures and then a successful promotion.
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="promote",
        # last_qualification reflects only the final promoted state —
        # the two prior infra failures are gone from here.
        last_qualification={
            "status": "promoted",
            "finalized_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Infra attempt 1 — audit_report_missing
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="infra_failure",
            finalized_at=finalized_at,
            sub_scores={"infra_failure": {"error": "audit_report_missing", "consecutive_infra_errors": 1, "same_error_as_prior": False, "same_error_different_candidate": False}},
        )
    )
    # Infra attempt 2 — same error, same candidate
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="infra_failure",
            finalized_at=finalized_at,
            sub_scores={"infra_failure": {"error": "audit_report_missing", "consecutive_infra_errors": 2, "same_error_as_prior": True, "same_error_different_candidate": False}},
        )
    )
    # Successful promotion verdict — overwrites last_qualification in production
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            terminal_status="promoted",
            finalized_at=now - timedelta(hours=1),
            sub_scores=None,
        )
    )
    await db_session.commit()

    result = await compute_release_qualifier_infra_metrics(
        db_session,
        project_id=project.id,
        window_start=now - timedelta(hours=24),
    )
    # All 3 verdict rows (2 infra + 1 promoted) count toward total runs.
    assert result["qualifier_runs_total"] == 3
    # Both infra verdicts are counted even though last_qualification only shows promoted.
    assert result["release_infra_failure_total"] == 2
    assert result["audit_report_missing_count"] == 2
    # The second infra attempt had same_error_as_prior=True.
    assert result["release_infra_failure_repeat_signature_total"] == 1
    assert result["release_infra_failure_streak_max"] == 2


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

    def test_trial_arms_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_trial_arms as digest_trial_arms

        for name in (
            "compute_trial_arm_accumulation",
            "compute_trial_arm_accrual",
            "_json_truthy",
        ):
            assert hasattr(digest_trial_arms, name)
            assert getattr(digest_metrics, name) is getattr(digest_trial_arms, name)

    def test_outer_loop_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_outer_loop as digest_outer_loop

        for name in (
            "_outer_loop_pct",
            "_outer_loop_chain_key",
            "_outer_loop_terminal_outcome",
            "_group_outer_loop_chains",
            "_scan_escalation_alerts",
            "_resolve_chain_outcome",
            "_outer_loop_chain_observation",
            "_resolve_outer_loop_chains",
            "_ESCALATION_OUTCOME_KEYS",
        ):
            assert hasattr(digest_outer_loop, name)
            assert getattr(digest_metrics, name) is getattr(digest_outer_loop, name)

    def test_lane_waste_canonical_home_and_reexport(self):
        import app.digest_metrics as digest_metrics
        import app.digest_lane_waste as digest_lane_waste

        for name in (
            "LANE_WASTE_CATEGORIES",
            "classify_lane_waste_category",
            "_lane_waste_task_row",
            "compute_lane_waste_diagnostics",
            "compute_lane_baseline_trend",
        ):
            assert hasattr(digest_lane_waste, name)
            assert getattr(digest_metrics, name) is getattr(digest_lane_waste, name)

    def test_extracted_modules_do_not_import_digest_metrics_at_load_time(self):
        """The new sibling modules must not import ``digest_metrics`` at module
        load time — that would create a circular import (digest_metrics imports
        them). Cross-module needs are satisfied via function-local imports."""
        import ast
        import os

        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        for module_file in (
            "digest_remediation.py",
            "digest_direction_progress.py",
            "digest_trial_arms.py",
            "digest_outer_loop.py",
            "digest_lane_waste.py",
        ):
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


# ── Lane-waste convergence evidence ────────────────────────────────────────


def test_lane_waste_reports_retarget_scope_and_exact_convergence_stats():
    """Lane waste keeps exact merged-chain stats without serializing raw samples."""
    window_start = _utcnow() - timedelta(days=1)
    direction_id = "direction-retargeted"

    merged_original = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "direction_id": direction_id},
    )
    merged_review = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "pr_number": 901}],
        context={
            "direction_id": direction_id,
            "review_loop": {
                "original_task_id": str(merged_original.id),
                "pr_number": 901,
                "iteration": 1,
                "retarget_scope": "delta",
            },
        },
    )
    merged_fix = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "fix", "pr_number": 901}],
        context={
            "review_loop": {
                "original_task_id": str(merged_original.id),
                "pr_number": 901,
                "iteration": 2,
            }
        },
    )
    merged_rereview = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "pr_number": 901}],
        context={
            "_review_stale_head": True,
            "review_loop": {
                "original_task_id": str(merged_original.id),
                "pr_number": 901,
                "iteration": 3,
                "retarget_scope": "full",
            },
        },
    )

    open_original = make_task(status=TaskStatus.COMPLETED, context={})
    open_review = make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "review", "pr_number": 902}],
        context={
            "_review_stale_target": True,
            "review_loop": {
                "original_task_id": str(open_original.id),
                "pr_number": 902,
                "iteration": 1,
                "retarget_scope": "full",
            },
        },
    )

    result = compute_lane_waste_diagnostics(
        [
            merged_original,
            merged_review,
            merged_fix,
            merged_rereview,
            open_original,
            open_review,
        ],
        window_start,
    )

    assert result["stale_head_discard_count"] == 1
    assert result["retarget_review_scope_counts"] == {"delta": 1, "full": 2}
    assert "converged_review_counts" not in result
    assert result["reviews_to_converge"] == {"count": 1, "median": 2, "p95": 2}

    merged_chain = next(
        row for row in result["chains"] if row["pr_number"] == 901
    )
    assert merged_chain["direction_id"] == direction_id
    assert merged_chain["review_count"] == 2
    assert merged_chain["reviews_to_converge"] == 2
    assert merged_chain["task_count"] == 3


def test_lane_waste_splits_reviews_to_converge_by_supersede_and_counts_cap_lost_directions():
    """Supersedes stay one chain, while cap loss counts each attributed direction once."""
    window_start = _utcnow() - timedelta(days=1)

    def _chain(*, pr_numbers, outcome, direction_id=None, direction_text=None, rollup=False):
        original_context = {"_is_rollup_review": True} if rollup else {}
        if outcome == "merged":
            original_context["pr_merged"] = True
        if direction_id is not None:
            original_context["direction_id"] = direction_id
        if direction_text is not None:
            original_context["direction_text"] = direction_text
        original = make_task(status=TaskStatus.COMPLETED, context=original_context)
        tasks = [original]
        for index, pr_number in enumerate(pr_numbers, start=1):
            is_terminal_cap = outcome == "cap_exhausted" and index == len(pr_numbers)
            context = {
                "review_loop": {
                    "original_task_id": str(original.id),
                    "pr_number": pr_number,
                    "iteration": index,
                }
            }
            if rollup:
                context["rollup_pr_number"] = pr_number
            if is_terminal_cap:
                context["failure_detail"] = "review_cap_exhausted"
            if direction_id is not None:
                context["direction_id"] = direction_id
            if direction_text is not None:
                context["direction_text"] = direction_text
            tasks.append(
                make_task(
                    status=TaskStatus.FAILED if is_terminal_cap else TaskStatus.COMPLETED,
                    payloads=[{"type": "review", "pr_number": pr_number}],
                    context=context,
                )
            )
        return tasks

    superseded = _chain(pr_numbers=["901", "902"], outcome="merged", rollup=True)
    same_pr = _chain(pr_numbers=["903", "903"], outcome="merged")
    same_pr[2].context["review_loop"]["head_drift_retargeted_from"] = {
        "pr_number": 700,
        "head_sha": "a" * 40,
    }
    cap_one = _chain(
        pr_numbers=["904"],
        outcome="cap_exhausted",
        direction_id="d-shared",
        direction_text="Shared cap direction",
    )
    cap_two = _chain(
        pr_numbers=["905"],
        outcome="cap_exhausted",
        direction_id="d-shared",
        direction_text="Shared cap direction",
    )
    cap_missing = _chain(pr_numbers=["906"], outcome="cap_exhausted")
    control = _chain(pr_numbers=["907"], outcome="merged", rollup=True)

    all_tasks = superseded + same_pr + cap_one + cap_two + cap_missing + control
    result = compute_lane_waste_diagnostics(all_tasks, window_start)

    assert result["supersede_affected_chain_count"] == 2
    assert result["supersede_affected_reviews_to_converge"] == {
        "count": 2,
        "sample_count": 2,
        "median": 2,
        "p95": 2,
    }
    assert result["cap_exhausted_chain_count"] == 3
    assert result["cap_exhausted_direction_count"] == 1
    assert result["cap_exhausted_directions"] == [
        {"direction_id": "d-shared", "direction_text": "Shared cap direction"}
    ]
    assert result["rollup_chain_count"] == 2
    assert result["rollup_supersede_affected_chain_count"] == 1
    assert result["rollup_supersede_unaffected_chain_count"] == 1
    assert result["rollup_supersede_affected_reviews_to_converge"] == {
        "count": 1,
        "sample_count": 1,
        "median": 2,
        "p95": 2,
    }
    assert result["rollup_supersede_unaffected_reviews_to_converge"] == {
        "count": 1,
        "sample_count": 1,
        "median": 1,
        "p95": 1,
    }
    cap_summary = compute_review_cap_exhaustion_summary(
        [task for task in all_tasks if task.status == TaskStatus.FAILED],
        window_start=window_start,
    )
    assert cap_summary["distinct_direction_count"] == 1
    superseded_row = next(
        row for row in result["chains"] if row["pr_number"] == "901"
    )
    assert superseded_row["supersede_affected"] is True
    assert superseded_row["review_loop_pr_numbers"] == ["901", "902"]
    same_pr_row = next(row for row in result["chains"] if row["pr_number"] == "903")
    assert same_pr_row["supersede_affected"] is True
    assert same_pr_row["review_loop_pr_numbers"] == ["700", "903"]


def test_lane_waste_retained_supersede_lineage_survives_window_boundary():
    """A replacement that converges next version remains supersede-affected."""
    window_start = _utcnow()
    original = make_task(
        status=TaskStatus.COMPLETED,
        created_at=window_start - timedelta(days=1),
        context={"pr_merged": True},
    )
    replacement_review = make_task(
        status=TaskStatus.COMPLETED,
        created_at=window_start + timedelta(minutes=1),
        payloads=[{"type": "review", "pr_number": 902}],
        context={
            "review_loop": {
                "original_task_id": str(original.id),
                "pr_number": 902,
                "iteration": 5,
                "superseded_pr_numbers": ["901"],
            }
        },
    )

    result = compute_lane_waste_diagnostics(
        [original, replacement_review], window_start
    )

    assert result["supersede_affected_chain_count"] == 1
    assert result["supersede_affected_reviews_to_converge"] == {
        "count": 1,
        "sample_count": 1,
        "median": 1,
        "p95": 1,
    }
    assert result["chains"][0]["review_loop_pr_numbers"] == ["901", "902"]


def test_lane_waste_supersede_percentiles_include_all_affected_chains():
    """Percentiles stay population metrics above the former 1,000-row cap."""
    window_start = _utcnow() - timedelta(days=1)
    tasks = []
    for chain_number in range(1_060):
        original = make_task(status=TaskStatus.COMPLETED, context={"pr_merged": True})
        tasks.append(original)
        review_count = 1 if chain_number < 1_000 else 10
        tasks.extend(
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review", "pr_number": chain_number + 2_000}],
                context={
                    "review_loop": {
                        "original_task_id": str(original.id),
                        "pr_number": chain_number + 2_000,
                        "iteration": iteration + 1,
                        "superseded_pr_numbers": [str(chain_number + 1_000)],
                    }
                },
            )
            for iteration in range(review_count)
        )

    result = compute_lane_waste_diagnostics(tasks, window_start)

    assert result["supersede_affected_reviews_to_converge"] == {
        "count": 1_060,
        "sample_count": 1_060,
        "median": 1,
        "p95": 10,
    }


def test_outer_loop_supersede_percentiles_include_all_affected_chains():
    """Convergence percentiles must not truncate durable snapshot inputs."""
    window_start = _utcnow() - timedelta(days=1)
    tasks = []
    for chain_number in range(1_060):
        original = make_task(status=TaskStatus.COMPLETED, context={"pr_merged": True})
        tasks.append(original)
        review_count = 1 if chain_number < 1_000 else 10
        tasks.extend(
            make_task(
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "review", "pr_number": chain_number + 4_000}],
                context={
                    "review_loop": {
                        "original_task_id": str(original.id),
                        "pr_number": chain_number + 4_000,
                        "iteration": iteration + 1,
                        "superseded_pr_numbers": [str(chain_number + 3_000)],
                    }
                },
            )
            for iteration in range(review_count)
        )

    result = compute_outer_loop_convergence(
        all_tasks=tasks, escalation_alerts=[], window_start=window_start
    )

    assert result["supersede_affected_reviews_to_converge"] == {
        "count": 1_060,
        "sample_count": 1_060,
        "median": 1,
        "p95": 10,
    }


# ── Auto-pause epidemic summary ────────────────────────────────────────────


def test_auto_pause_epidemic_summary_zero_when_no_pauses():
    """Directions with no auto_pause_count produce zeroed summary."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    directions = [
        {"id": "d-aaa", "text": "Direction A"},
        {"id": "d-bbb", "text": "Direction B"},
        {"id": "d-ccc", "text": "Direction C", "auto_pause_count": 0},
    ]
    result = _compute_auto_pause_epidemic_summary(directions)
    assert result["total_cycles"] == 0
    assert result["affected_directions"] == 0
    assert result["top_offenders"] == []


def test_auto_pause_epidemic_summary_aggregates_correctly():
    """Epidemic summary correctly sums cycles and ranks top offenders."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    directions = [
        {"id": "d-aaa", "text": "Alpha direction", "auto_pause_count": 10},
        {"id": "d-bbb", "text": "Beta direction", "auto_pause_count": 3},
        {"id": "d-ccc", "text": "Gamma direction", "auto_pause_count": 0},
        {"id": "d-ddd", "text": "Delta direction"},
        {"id": "d-eee", "text": "Epsilon direction", "auto_pause_count": 7},
    ]
    result = _compute_auto_pause_epidemic_summary(directions)

    assert result["total_cycles"] == 20
    assert result["affected_directions"] == 3
    # Top offenders sorted descending by auto_pause_count
    offenders = result["top_offenders"]
    assert len(offenders) == 3
    assert offenders[0]["direction_id"] == "d-aaa"
    assert offenders[0]["auto_pause_count"] == 10
    assert offenders[1]["direction_id"] == "d-eee"
    assert offenders[1]["auto_pause_count"] == 7
    assert offenders[2]["direction_id"] == "d-bbb"
    assert offenders[2]["auto_pause_count"] == 3
    # Each offender entry has required keys
    for entry in offenders:
        assert "direction_id" in entry
        assert "direction_text" in entry
        assert "auto_pause_count" in entry


def test_auto_pause_epidemic_summary_caps_top_offenders_at_five():
    """top_offenders list is capped at 5 entries even with many paused directions."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    directions = [
        {"id": f"d-{i}", "text": f"Direction {i}", "auto_pause_count": i + 1}
        for i in range(8)
    ]
    result = _compute_auto_pause_epidemic_summary(directions)
    assert result["total_cycles"] == sum(range(1, 9))
    assert result["affected_directions"] == 8
    assert len(result["top_offenders"]) == 5
    # Highest counts appear first
    assert result["top_offenders"][0]["auto_pause_count"] == 8


def test_auto_pause_epidemic_summary_direction_text_truncated_to_80():
    """direction_text in top_offenders is truncated to 80 characters."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    long_text = "X" * 120
    directions = [{"id": "d-long", "text": long_text, "auto_pause_count": 5}]
    result = _compute_auto_pause_epidemic_summary(directions)
    assert len(result["top_offenders"][0]["direction_text"]) == 80


def test_auto_pause_epidemic_summary_uses_direction_id_fallback():
    """Directions using direction_id instead of id are correctly handled."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    directions = [
        {"direction_id": "d-old-style", "text": "Old style", "auto_pause_count": 4}
    ]
    result = _compute_auto_pause_epidemic_summary(directions)
    assert result["total_cycles"] == 4
    assert result["top_offenders"][0]["direction_id"] == "d-old-style"


def test_auto_pause_epidemic_summary_churn_family_pause_count_is_primary_sort_key():
    """A direction with higher churn_family_pause_count ranks above one with
    higher auto_pause_count — proves zombie counter is the primary sort key."""
    from app.digest_direction_progress import _compute_auto_pause_epidemic_summary

    directions = [
        # d-zombie: lower auto_pause_count but higher churn_family_pause_count
        {
            "id": "d-zombie",
            "text": "Zombie direction",
            "auto_pause_count": 2,
            "churn_family_pause_count": 5,
        },
        # d-high-pauses: higher auto_pause_count but zero churn_family_pause_count
        {
            "id": "d-high-pauses",
            "text": "High pause direction",
            "auto_pause_count": 10,
            "churn_family_pause_count": 0,
        },
    ]
    result = _compute_auto_pause_epidemic_summary(directions)

    offenders = result["top_offenders"]
    assert len(offenders) == 2
    # d-zombie must rank first despite lower auto_pause_count
    assert offenders[0]["direction_id"] == "d-zombie"
    assert offenders[0]["churn_family_pause_count"] == 5
    assert offenders[1]["direction_id"] == "d-high-pauses"
    # surfaced field is present on each entry
    for entry in offenders:
        assert "churn_family_pause_count" in entry


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


def test_parked_blocker_summary_includes_recut_escalated_tasks():
    """Tasks with ``_recut_escalated`` marker (persistent re-cut conflicts) are
    included in the parked summary alongside BLOCKED-escalated tasks. Only
    escalated re-cuts are included; a still-retrying re-cut (no escalation) is
    excluded even if it has re-cut context."""
    from app.digest_metrics import compute_parked_blocker_summary

    now = _utcnow()
    recut_conflict_escalated = make_task(
        status=TaskStatus.QUEUED,
        title="dependent-held-by-conflict",
        created_at=now - timedelta(hours=6),
        recheck_after=now + timedelta(days=1),
        context={
            "_recut_escalated": True,
            "_recut_recheck_attempts": 5,
            "_recut_last_outcome": "conflict",
            "_recut_conflict_attempts": 3,
        },
    )
    recut_still_retrying = make_task(
        status=TaskStatus.QUEUED,
        title="dependent-held-by-error",
        created_at=now - timedelta(hours=2),
        context={
            "_recut_recheck_attempts": 2,
            "_recut_last_outcome": "error",
            "_recut_conflict_attempts": 0,
            # No _recut_escalated marker
        },
    )
    not_held = make_task(
        status=TaskStatus.QUEUED,
        title="normal-queued",
        context={"_blocked_recheck_attempts": 1},  # Different context, no re-cut
    )

    result = compute_parked_blocker_summary(
        [recut_conflict_escalated, recut_still_retrying, not_held]
    )

    assert result["total"] == 1  # Only the escalated re-cut
    assert len(result["tasks"]) == 1
    # Verify conflict is present
    conflict_row = result["tasks"][0]
    assert conflict_row["title"] == "dependent-held-by-conflict"
    assert conflict_row["blocker_kind"] == "Base re-cut conflict"
    # Verify still-retrying is excluded
    retrying_row = next(
        (t for t in result["tasks"] if t["title"] == "dependent-held-by-error"), None
    )
    assert retrying_row is None


@pytest.mark.asyncio
async def test_dependency_health_signals_query_surfaces_recut_escalated(db_session):
    """Regression: the DB-backed query in ``compute_dependency_health_signals``
    must feed BOTH parked classes to the pure summarizer — not just
    BLOCKED+``_blocked_escalated`` tasks but also ``_recut_escalated`` tasks,
    which the producer stamps on QUEUED (or BLOCKED-without-``_blocked_escalated``)
    dependents. A previous version filtered to ``status == BLOCKED AND
    _blocked_escalated`` only, making the summarizer's re-cut branch unreachable
    in production even though the unit test passed by calling the pure function
    directly."""
    project = make_project(name="Dep health query test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # QUEUED dependent held by a persistent re-cut conflict (no _blocked_escalated).
    recut_held = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        title="recut-held-dependent",
        context={
            "_recut_escalated": True,
            "_recut_recheck_attempts": 5,
            "_recut_last_outcome": "conflict",
            "_recut_conflict_attempts": 3,
        },
    )
    # BLOCKED + operator-escalated block.
    blocked_escalated = make_task(
        project_id=project.id,
        status=TaskStatus.BLOCKED,
        title="blocked-escalated-dependent",
        context={"_blocked_escalated": True},
    )
    # Neither marker → must be excluded.
    plain_queued = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        title="plain-queued",
        context={"_blocked_recheck_attempts": 1},
    )
    db_session.add_all([recut_held, blocked_escalated, plain_queued])
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    parked = result["parked_blockers"]
    titles = {t["title"] for t in parked["tasks"]}
    assert parked["total"] == 2
    assert titles == {"recut-held-dependent", "blocked-escalated-dependent"}
    assert "plain-queued" not in titles


def test_unresumable_cancellation_summary_counts_and_buckets():
    """The pure summarizer counts terminal give-up cancellations and breaks them
    down by give-up class and by originating direction; tasks with no marker are
    skipped."""
    from app.digest_metrics import compute_unresumable_cancellation_summary
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_FOUNDATION_DIED,
        GIVE_UP_NEVER_SATISFIABLE,
    )

    def _cancelled(give_up_class, direction_id, at):
        return make_task(
            status=TaskStatus.CANCELLED,
            context={
                UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                    "give_up_class": give_up_class,
                    "need": {"kind": "capability", "ref": "x"},
                    "direction_id": direction_id,
                    "at": at,
                }
            },
        )

    died_a = _cancelled(GIVE_UP_FOUNDATION_DIED, "d-aaa", "2026-06-04T10:00:00")
    died_b = _cancelled(GIVE_UP_FOUNDATION_DIED, "d-bbb", "2026-06-04T11:00:00")
    never_a = _cancelled(GIVE_UP_NEVER_SATISFIABLE, "d-aaa", "2026-06-04T12:00:00")
    # No marker → excluded.
    plain = make_task(status=TaskStatus.CANCELLED, context={})

    result = compute_unresumable_cancellation_summary([died_a, died_b, never_a, plain])

    assert result["total"] == 3
    assert result["by_class"] == {
        GIVE_UP_FOUNDATION_DIED: 2,
        GIVE_UP_NEVER_SATISFIABLE: 1,
    }
    assert result["by_direction"] == {"d-aaa": 2, "d-bbb": 1}
    # Newest-first.
    assert result["tasks"][0]["task_id"] == str(never_a.id)


def test_unresumable_cancellation_summary_filters_by_window():
    """A marker whose ``at`` precedes ``window_start`` is excluded from the count."""
    from app.digest_metrics import compute_unresumable_cancellation_summary
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_FOUNDATION_DIED,
    )

    recent = make_task(
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_FOUNDATION_DIED,
                "need": None,
                "direction_id": "d-aaa",
                "at": "2026-06-04T10:00:00",
            }
        },
    )
    stale = make_task(
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_FOUNDATION_DIED,
                "need": None,
                "direction_id": "d-aaa",
                "at": "2026-05-01T10:00:00",
            }
        },
    )

    result = compute_unresumable_cancellation_summary(
        [recent, stale], window_start=datetime(2026, 6, 1, 0, 0, 0)
    )

    assert result["total"] == 1
    assert result["tasks"][0]["task_id"] == str(recent.id)


@pytest.mark.asyncio
async def test_dependency_health_signals_query_surfaces_unresumable_cancellations(
    db_session,
):
    """The DB-backed dependency-health query surfaces the unresumable-cancellation
    aggregate: CANCELLED tasks carrying the marker within the recent window are
    counted; an unmarked CANCELLED task is excluded."""
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_FOUNDATION_DIED,
    )

    project = make_project(name="Unresumable cancel query", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    marked = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        title="gave-up-dependent",
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_FOUNDATION_DIED,
                "need": {"kind": "task", "ref": "foo"},
                "direction_id": "d-aaa",
                "at": _utcnow().isoformat(),
            }
        },
    )
    unmarked = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        title="plain-cancelled",
        context={},
    )
    db_session.add_all([marked, unmarked])
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    agg = result["unresumable_cancellations"]
    assert agg["total"] == 1
    assert agg["by_class"] == {GIVE_UP_FOUNDATION_DIED: 1}
    assert agg["by_direction"] == {"d-aaa": 1}
    assert agg["tasks"][0]["title"] == "gave-up-dependent"


@pytest.mark.asyncio
async def test_dependency_health_signals_query_surfaces_finding_structural_give_up(
    db_session,
):
    """The dependency-health rollup counts research findings that were finally dropped."""
    project = make_project(name="Finding give-up query", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dropped = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        title="research cycle with give-up",
        context={
            "finding_structural_give_up": {
                "at": "2026-06-04T12:00:00",
                "total": 2,
                "by_reason": {"prereq_deferred_same_cycle": 2},
                "by_direction": {"d-aaa": 2},
                "sample": [
                    {
                        "title": "Use shared helper",
                        "reason": "prereq_deferred_same_cycle",
                        "direction_id": "d-aaa",
                    }
                ],
            }
        },
    )
    plain = make_task(project_id=project.id, status=TaskStatus.COMPLETED, title="plain")
    db_session.add_all([dropped, plain])
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    agg = result["finding_structural_give_up"]
    assert agg["total"] == 2
    assert agg["by_reason"] == {"prereq_deferred_same_cycle": 2}
    assert agg["by_direction"] == {"d-aaa": 2}
    assert agg["findings"][0]["title"] == "Use shared helper"


def test_live_blocked_summary_counts_and_buckets_by_age():
    """The live-blocked summarizer counts EVERY currently-BLOCKED task (not just
    parked/escalated ones) and buckets them by how long they have been blocked."""
    from app.digest_metrics import compute_live_blocked_summary

    now = _utcnow()

    def _blocked(age_hours, direction_id=None):
        return make_task(
            status=TaskStatus.BLOCKED,
            title=f"blocked-{age_hours}h",
            blocked_at=now - timedelta(hours=age_hours),
            context={"direction_id": direction_id} if direction_id else {},
        )

    fresh = _blocked(0.5, "d-aaa")  # <1h
    mid = _blocked(3, "d-aaa")  # 1-6h
    older = _blocked(12)  # 6-24h
    ancient = _blocked(48)  # >24h
    # No blocked_at → unknown bucket, still counted.
    no_ts = make_task(status=TaskStatus.BLOCKED, title="no-ts", blocked_at=None)

    result = compute_live_blocked_summary([fresh, mid, older, ancient, no_ts])

    assert result["total"] == 5
    assert result["by_age"] == {
        "<1h": 1,
        "1-6h": 1,
        "6-24h": 1,
        ">24h": 1,
        "unknown": 1,
    }
    # Newest-blocked first: the 0.5h task leads (excluding the no-ts null sort).
    titles = [t["title"] for t in result["tasks"]]
    assert "blocked-0.5h" in titles
    assert result["tasks"][0]["title"] == "blocked-0.5h"


def test_blocked_resume_summary_counts_and_breaks_down_by_resolved_by():
    """The pure resume summarizer counts resume markers and breaks them down by
    ``resolved_by`` (edge vs completed) and by originating direction; tasks with no
    marker are skipped."""
    from app.digest_metrics import compute_blocked_resume_summary
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    def _resumed(resolved_by, direction_id, at):
        return make_task(
            status=TaskStatus.COMPLETED,
            context={
                RESUME_MARKER_CONTEXT_KEY: {
                    "resolved_by": resolved_by,
                    "foundation": ["f-1"],
                    "direction_id": direction_id,
                    "at": at,
                }
            },
        )

    edge_a = _resumed("edge", "d-aaa", "2026-06-04T10:00:00")
    edge_b = _resumed("edge", "d-bbb", "2026-06-04T11:00:00")
    completed_a = _resumed("completed", "d-aaa", "2026-06-04T12:00:00")
    # No marker → excluded.
    plain = make_task(status=TaskStatus.QUEUED, context={})

    result = compute_blocked_resume_summary([edge_a, edge_b, completed_a, plain])

    assert result["total"] == 3
    assert result["by_resolved_by"] == {"edge": 2, "completed": 1}
    assert result["by_direction"] == {"d-aaa": 2, "d-bbb": 1}
    # Newest-first.
    assert result["tasks"][0]["task_id"] == str(completed_a.id)


def test_blocked_resume_summary_filters_by_window_and_empty_case():
    """A marker whose ``at`` precedes ``window_start`` is excluded; an all-stale set
    yields an empty (zeroed) summary."""
    from app.digest_metrics import compute_blocked_resume_summary
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    def _resumed(at):
        return make_task(
            status=TaskStatus.COMPLETED,
            context={
                RESUME_MARKER_CONTEXT_KEY: {
                    "resolved_by": "edge",
                    "foundation": ["f-1"],
                    "direction_id": "d-aaa",
                    "at": at,
                }
            },
        )

    recent = _resumed("2026-06-04T10:00:00")
    stale = _resumed("2026-05-01T10:00:00")

    result = compute_blocked_resume_summary(
        [recent, stale], window_start=datetime(2026, 6, 1, 0, 0, 0)
    )
    assert result["total"] == 1
    assert result["tasks"][0]["task_id"] == str(recent.id)

    # Empty case: every marker stale → zeroed summary.
    empty = compute_blocked_resume_summary(
        [stale], window_start=datetime(2026, 6, 1, 0, 0, 0)
    )
    assert empty["total"] == 0
    assert empty["by_resolved_by"] == {}
    assert empty["tasks"] == []


@pytest.mark.asyncio
async def test_dependency_health_signals_query_surfaces_live_blocked(db_session):
    """The DB-backed query surfaces the live BLOCKED population — including a
    not-yet-escalated dependent the parked-blocker signal deliberately omits."""
    project = make_project(name="Live blocked query", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    waiting = make_task(
        project_id=project.id,
        status=TaskStatus.BLOCKED,
        title="healthy-waiting-dependent",
        blocked_at=_utcnow() - timedelta(hours=2),
        context={"direction_id": "d-aaa"},  # no _blocked_escalated
    )
    queued = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        title="not-blocked",
    )
    db_session.add_all([waiting, queued])
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    live = result["live_blocked"]
    assert live["total"] == 1
    assert live["tasks"][0]["title"] == "healthy-waiting-dependent"
    assert live["by_age"] == {"1-6h": 1}
    # The not-yet-escalated waiter is NOT in the parked-only signal.
    assert result["parked_blockers"]["total"] == 0


@pytest.mark.asyncio
async def test_dependency_health_signals_query_surfaces_blocked_resumes(db_session):
    """The DB-backed query is STATUS-AGNOSTIC for resume markers: a resumed task
    that has advanced past QUEUED (e.g. to DONE) still carries the marker and is
    counted within the window; an unmarked task is excluded."""
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    project = make_project(name="Blocked resume query", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Status-agnostic proof: marker present on a DONE (non-QUEUED) task.
    resumed_done = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        title="resumed-then-done",
        context={
            RESUME_MARKER_CONTEXT_KEY: {
                "resolved_by": "completed",
                "foundation": [{"kind": "task", "ref": "foo"}],
                "direction_id": "d-aaa",
                "at": _utcnow().isoformat(),
            }
        },
    )
    unmarked = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        title="plain-done",
        context={},
    )
    db_session.add_all([resumed_done, unmarked])
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    agg = result["resumes"]
    assert agg["total"] == 1
    assert agg["by_resolved_by"] == {"completed": 1}
    assert agg["by_direction"] == {"d-aaa": 1}
    assert agg["tasks"][0]["title"] == "resumed-then-done"


def _blocked_dependent(project_id, need, *, direction_id=None, blocked_at=None):
    """A BLOCKED dependent waiting on a single machine-resolvable foundation need —
    mirrors the producer's expected ``blocker.needs`` shape (see
    test_capability_demand._blocked)."""
    ctx = {
        "blocker": {
            "source": "agent",
            "waiting_on": need.get("ref"),
            "reason": "BLOCKER: waiting on foundation",
            "need": need,
            "needs": [need],
        },
    }
    if direction_id is not None:
        ctx["direction_id"] = direction_id
    return make_task(
        project_id=project_id,
        status=TaskStatus.BLOCKED,
        blocked_at=blocked_at or (_utcnow() - timedelta(hours=4)),
        context=ctx,
    )


@pytest.mark.asyncio
async def test_missing_capability_demand_summary_aggregates_blocked_and_cancelled(
    db_session,
):
    """The summary folds live-blocked (still_blocked) and recently give-up-CANCELLED
    (already_cancelled) dependents of the same capability into one demand, exposing
    the split, the per-direction count, and a largest-first sample."""
    from app.digest_metrics import compute_missing_capability_demand_summary
    from app.reaper_sweeps import (
        GIVE_UP_NEVER_SATISFIABLE,
        MISSING_FOUNDATION_MARKER_KEY,
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
    )

    project = make_project(name="Missing demand summary", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    cap_need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth"}

    # Two live BLOCKED dependents of the same capability under different directions.
    live_a = _blocked_dependent(
        project.id,
        cap_need,
        direction_id="d-live",
        blocked_at=now - timedelta(hours=10),
    )
    live_b = _blocked_dependent(
        project.id,
        cap_need,
        direction_id="d-live2",
        blocked_at=now - timedelta(hours=3),
    )
    # A give-up-CANCELLED dependent retired as missing-foundation within the window.
    cancelled = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        context={
            UNRESUMABLE_CANCEL_CONTEXT_KEY: {
                "give_up_class": GIVE_UP_NEVER_SATISFIABLE,
                "need": cap_need,
                "direction_id": "d-dead",
                "at": (now - timedelta(hours=2)).isoformat(),
                MISSING_FOUNDATION_MARKER_KEY: True,
            }
        },
    )
    db_session.add_all([live_a, live_b, cancelled])
    await db_session.commit()

    summary = await compute_missing_capability_demand_summary(
        db_session, project_id=project.id, now=now
    )

    assert summary["total"] == 1
    assert summary["still_blocked"] == 2
    assert summary["already_cancelled"] == 1
    # by_direction counts how many demands each originating direction carries.
    assert summary["by_direction"] == {"d-live": 1, "d-live2": 1, "d-dead": 1}
    entry = summary["demands"][0]
    assert entry["kind"] == "capability"
    assert entry["dependent_count"] == 3
    assert entry["oldest_blocked_at"] == (now - timedelta(hours=10)).isoformat()


@pytest.mark.asyncio
async def test_dependency_health_signals_includes_missing_capability_demand(db_session):
    """The dependency-health rollup nests the missing-capability-demand summary so the
    digest router emits it with no router edit — a real named consumer."""
    project = make_project(name="Missing demand rollup", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cap_need = {"kind": "capability", "ref": "billing-core", "label": "Billing"}
    blocked = _blocked_dependent(project.id, cap_need, direction_id="d-aaa")
    db_session.add(blocked)
    await db_session.commit()

    result = await compute_dependency_health_signals(db_session, project_id=project.id)

    assert "missing_capability_demand" in result
    agg = result["missing_capability_demand"]
    assert agg["total"] == 1
    assert agg["still_blocked"] == 1
    assert agg["already_cancelled"] == 0
    assert agg["by_direction"] == {"d-aaa": 1}


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


def test_duplicate_foundation_signal_includes_qa_docs_deps():
    """QA/docs/deps write-tasks that produce PRs ARE included in duplicate-foundation
    detection (not limited to SR code-change types create/fix/refactor/migrate)."""
    from app.digest_metrics import compute_duplicate_foundation_signal

    files = ["app/foundation.py", "app/db.py"]
    p_a, p_b, p_c, p_d, p_e = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()

    # Each task gets a unique parent so they're all flagged as cross-parent duplicates
    qa_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_a,
        payloads=[{"type": "qa", "related_files": files}],
    )
    create_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_b,
        payloads=[{"type": "create", "related_files": files}],
    )
    docs_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_c,
        payloads=[{"type": "docs", "related_files": files}],
    )
    fix_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_d,
        payloads=[{"type": "fix", "related_files": files}],
    )
    deps_task = make_task(
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=p_e,
        payloads=[{"type": "deps", "related_files": files}],
    )

    result = compute_duplicate_foundation_signal([qa_task, create_task, docs_task, fix_task, deps_task])

    # With 5 tasks all sharing the same files and different parents, we get C(5,2) = 10 pairs
    assert result["total"] == 10
    # All candidates should have strong overlap (all share identical files)
    for cand in result["candidates"]:
        assert cand["overlap_ratio"] == 1.0
        assert cand["shared_files"] == ["app/db.py", "app/foundation.py"]


# ══════════════════════════════════════════════════════════════════════════════
# Section: Foundation Convergence Predicate Tests
# ══════════════════════════════════════════════════════════════════════════════


def test_genuine_duplicate_reason_shared_finding_origin():
    """Genuine duplicate: two write-tasks with identical finding_hash."""
    from app.digest_metrics import _genuine_duplicate_reason

    t1 = make_task(
        payloads=[{"type": "fix"}],
        context={"finding_hash": "abc123"},
    )
    t2 = make_task(
        payloads=[{"type": "fix"}],
        context={"finding_hash": "abc123"},
    )
    reason = _genuine_duplicate_reason(t1, set(), t2, set())
    assert reason is not None
    assert "shared finding origin" in reason
    assert "abc123" in reason


def test_genuine_duplicate_reason_shared_capability():
    """Genuine duplicate: two write-tasks with a shared capability marker."""
    from app.digest_metrics import _genuine_duplicate_reason

    t1 = make_task(
        payloads=[{"type": "create"}],
        context={"provides_capability": "linting"},
    )
    t2 = make_task(
        payloads=[{"type": "create"}],
        context={"provides_capability": "linting"},
    )
    reason = _genuine_duplicate_reason(t1, set(), t2, set())
    assert reason is not None
    assert "shared capability" in reason
    assert "linting" in reason


def test_genuine_duplicate_reason_near_identical_files_at_threshold():
    """Genuine duplicate: file overlap exactly at the 0.85 convergence threshold."""
    from app.digest_metrics import _genuine_duplicate_reason

    # Create exact threshold case: 85 shared, 15 unique to i, 0 unique to j = 85/100 = 0.85 >= 0.85
    shared_files = {f"file_{i}.py" for i in range(85)}
    fi = shared_files | {f"unique_i_{i}.py" for i in range(15)}
    fj = shared_files

    t1 = make_task(payloads=[{"type": "refactor"}])
    t2 = make_task(payloads=[{"type": "refactor"}])
    reason = _genuine_duplicate_reason(t1, fi, t2, fj)
    assert reason is not None
    assert "near-identical file sets" in reason
    overlap = 85 / 100
    assert f"{round(overlap, 3)}" in reason


def test_genuine_duplicate_reason_below_convergence_threshold_returns_none():
    """Below convergence threshold (0.85): strong-but-partial overlap is NOT genuine.

    This is the incidental-shared-infra case where tasks can safely run in parallel
    and reconcile at merge. They should NOT be serialized.
    """
    from app.digest_metrics import (
        _genuine_duplicate_reason,
        _FOUNDATION_CONVERGE_THRESHOLD,
    )

    # Create a case just below the threshold: 5 shared, 5 unique each = 5/10 = 0.50
    fi = {
        "a.py",
        "b.py",
        "c.py",
        "d.py",
        "e.py",
        "f1.py",
        "g1.py",
        "h1.py",
        "i1.py",
        "j1.py",
    }
    fj = {
        "a.py",
        "b.py",
        "c.py",
        "d.py",
        "e.py",
        "f2.py",
        "g2.py",
        "h2.py",
        "i2.py",
        "j2.py",
    }

    t1 = make_task(payloads=[{"type": "create"}])
    t2 = make_task(payloads=[{"type": "create"}])
    reason = _genuine_duplicate_reason(t1, fi, t2, fj)
    assert reason is None, (
        f"Overlap {len(fi & fj)}/{len(fi | fj)} should be below {_FOUNDATION_CONVERGE_THRESHOLD}"
    )


def test_genuine_duplicate_reason_prefers_earliest_signal():
    """Among multiple signals, any one is sufficient (shared finding > shared cap > file overlap)."""
    from app.digest_metrics import _genuine_duplicate_reason

    # Both have finding hash AND shared capability
    t1 = make_task(
        payloads=[{"type": "fix"}],
        context={"finding_hash": "xyz789", "provides_capability": "auth"},
    )
    t2 = make_task(
        payloads=[{"type": "fix"}],
        context={"finding_hash": "xyz789", "provides_capability": "auth"},
    )
    reason = _genuine_duplicate_reason(t1, set(), t2, set())
    # Should return the first match (finding hash)
    assert "shared finding origin" in reason


def test_compute_foundation_convergence_actions_shared_finding_origin():
    """Acting convergence predicate: genuine duplicate with shared finding origin."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    t1 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "fix"}],
        context={"finding_hash": "finding001"},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),  # Different parent
        payloads=[{"type": "fix"}],
        context={"finding_hash": "finding001"},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 1
    assert actions[0]["foundation_id"] == str(t1.id)
    assert actions[0]["dependent_id"] == str(t2.id)
    assert "finding001" in actions[0]["reason"]


def test_compute_foundation_convergence_actions_shared_capability():
    """Acting convergence predicate: genuine duplicate with shared capability."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    t1 = make_task(
        created_at=now - timedelta(hours=2),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"provides_capability": "performance-tuning"},
    )
    t2 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"provides_capability": "performance-tuning"},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 1
    assert actions[0]["foundation_id"] == str(t1.id)
    assert actions[0]["dependent_id"] == str(t2.id)
    assert "shared capability" in actions[0]["reason"]


def test_compute_foundation_convergence_actions_file_overlap_at_threshold():
    """Acting convergence predicate: high file overlap at/above convergence threshold."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    # File overlap at threshold: 6 shared, 0 unique in files_i, 0 unique in files_j
    # This gives intersection=6, union=6, ratio=1.0 >= 0.85
    # Actually, let's be more realistic: 6 shared, 1 unique to each = 6/(6+1+1)=6/8=0.75 < 0.85
    # So we need: shared=6, unique_i=0, unique_j=0 for ratio=1.0
    # Or: shared=17, unique_i=3, unique_j=3 for ratio=17/23=0.739 < 0.85
    # Let's use: shared=85, unique_i=15, unique_j=0 for ratio=85/100=0.85
    shared_files = {f"file_{i}.py" for i in range(85)}
    files_i = shared_files | {f"unique_i_{i}.py" for i in range(15)}
    files_j = shared_files

    t1 = make_task(
        created_at=now - timedelta(minutes=30),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "refactor"}],
        context={"related_files": list(files_i)},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "refactor"}],
        context={"related_files": list(files_j)},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 1
    assert actions[0]["foundation_id"] == str(t1.id)
    assert actions[0]["dependent_id"] == str(t2.id)
    assert "near-identical file sets" in actions[0]["reason"]
    # 85/100 = 0.85, exactly at threshold
    assert "0.85" in actions[0]["reason"]


def test_compute_foundation_convergence_actions_below_threshold_no_convergence():
    """Below convergence threshold: overlapping files do NOT trigger convergence."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    # Overlap: 50 shared, 50 unique to each = 50/100 = 0.5 << 0.85
    # This is the shared-infrastructure case where tasks should run in parallel
    shared_files = {f"shared/file_{i}.py" for i in range(50)}
    files_i = shared_files | {f"only_i_{i}.py" for i in range(50)}
    files_j = shared_files | {f"only_j_{i}.py" for i in range(50)}

    t1 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"related_files": list(files_i)},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"related_files": list(files_j)},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 0, "Below-threshold overlap should not trigger convergence"


def test_compute_foundation_convergence_actions_same_parent_excluded():
    """Same-parent siblings are excluded (handled by intra-decomposition depends_on)."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    parent_id = uuid4()
    # Both tasks have the same parent, so they should NOT be serialized by this path
    t1 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent_id,
        payloads=[{"type": "fix"}],
        context={"finding_hash": "same_finding"},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent_id,  # Same parent
        payloads=[{"type": "fix"}],
        context={"finding_hash": "same_finding"},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 0, "Same-parent siblings should be excluded"


def test_compute_foundation_convergence_actions_same_parent_included_when_opted_out():
    """exclude_same_parent=False (the spawn-time call) DOES converge same-parent
    genuine duplicates: a research batch's children all share one parent, so the
    coarse parent check would otherwise mask every within-batch duplicate."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    parent_id = uuid4()
    t1 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.QUEUED,
        parent_task_id=parent_id,
        payloads=[{"type": "fix"}],
        context={"finding_hash": "same_finding"},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.QUEUED,
        parent_task_id=parent_id,  # Same parent — included only when opted out.
        payloads=[{"type": "fix"}],
        context={"finding_hash": "same_finding"},
    )

    assert compute_foundation_convergence_actions([t1, t2]) == []
    actions = compute_foundation_convergence_actions(
        [t1, t2], exclude_same_parent=False
    )
    assert len(actions) == 1
    assert actions[0]["foundation_id"] == str(t1.id)
    assert actions[0]["dependent_id"] == str(t2.id)


def test_apply_foundation_convergence_action_wires_edge_and_stamps_marker():
    """The shared apply helper wires the depends_on edge and stamps the
    _foundation_converged marker, returning the mutated dependent."""
    from app.digest_metrics import (
        apply_foundation_convergence_action,
        compute_foundation_convergence_actions,
    )

    now = _utcnow()
    canonical = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.QUEUED,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "apply001"},
    )
    dependent = make_task(
        created_at=now,
        status=TaskStatus.QUEUED,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "apply001"},
    )
    by_id = {str(canonical.id): canonical, str(dependent.id): dependent}
    candidates = [canonical, dependent]
    [action] = compute_foundation_convergence_actions(candidates)

    result = apply_foundation_convergence_action(action, by_id, candidates)

    assert result is dependent
    assert str(canonical.id) in [str(d) for d in dependent.depends_on]
    marker = dependent.context.get("_foundation_converged", {})
    assert marker.get("foundation_id") == str(canonical.id)
    assert "apply001" in marker.get("reason", "")


def test_apply_foundation_convergence_action_refuses_self_and_existing_edge():
    """The apply helper is idempotent and refuses a self-edge: an already-present
    edge returns None and does not duplicate, and a self-referential action is
    rejected."""
    from app.digest_metrics import apply_foundation_convergence_action

    now = _utcnow()
    canonical = make_task(
        created_at=now - timedelta(hours=1), payloads=[{"type": "fix"}]
    )
    dependent = make_task(
        created_at=now,
        payloads=[{"type": "fix"}],
        depends_on=[str(canonical.id)],
    )
    by_id = {str(canonical.id): canonical, str(dependent.id): dependent}
    candidates = [canonical, dependent]

    # Already-present edge → no-op.
    existing_action = {
        "dependent_id": str(dependent.id),
        "foundation_id": str(canonical.id),
        "reason": "x",
        "shared_files": [],
    }
    assert (
        apply_foundation_convergence_action(existing_action, by_id, candidates) is None
    )
    assert dependent.depends_on == [str(canonical.id)]

    # Self-edge → refused.
    self_action = {
        "dependent_id": str(dependent.id),
        "foundation_id": str(dependent.id),
        "reason": "x",
        "shared_files": [],
    }
    assert apply_foundation_convergence_action(self_action, by_id, candidates) is None


def test_compute_foundation_convergence_actions_earlier_task_is_canonical():
    """Earlier-created task becomes canonical foundation; later depends on earlier."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    # t2 created first, but t1 will be created later so t2 is canonical
    t2 = make_task(
        created_at=now - timedelta(hours=2),
        id=uuid4(),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "finding_x"},
    )
    t1 = make_task(
        created_at=now - timedelta(hours=1),
        id=uuid4(),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "finding_x"},
    )

    actions = compute_foundation_convergence_actions([t2, t1])

    assert len(actions) == 1
    # t2 is earlier (created_at), so it's canonical
    assert actions[0]["foundation_id"] == str(t2.id)
    assert actions[0]["dependent_id"] == str(t1.id)


def test_compute_foundation_convergence_actions_id_tiebreaker_when_same_time():
    """When created_at is identical, use ID as tiebreaker (deterministic ordering)."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    t1 = make_task(
        created_at=now,
        id=uuid4(),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "fix"}],
        context={"provides_capability": "test"},
    )
    t2 = make_task(
        created_at=now,
        id=uuid4(),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "fix"}],
        context={"provides_capability": "test"},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 1
    # One should be canonical, the other dependent, but which depends on ID ordering
    assert actions[0]["foundation_id"] in (str(t1.id), str(t2.id))
    assert actions[0]["dependent_id"] in (str(t1.id), str(t2.id))
    assert actions[0]["foundation_id"] != actions[0]["dependent_id"]


def test_compute_foundation_convergence_actions_shared_files_populated():
    """Action payload includes shared files for visibility."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    # Create files with 85 shared, 15 unique to t1, 0 unique to t2 = 85/100 = 0.85 >= 0.85
    shared = [f"lib/core_{i}.py" for i in range(85)]
    unique_t1 = [f"lib/only_t1_{i}.py" for i in range(15)]

    t1 = make_task(
        created_at=now - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "refactor"}],
        context={"related_files": shared + unique_t1},
    )
    t2 = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "refactor"}],
        context={"related_files": shared},
    )

    actions = compute_foundation_convergence_actions([t1, t2])

    assert len(actions) == 1
    shared_set = set(actions[0]["shared_files"])
    assert shared_set == set(shared)
    for f in unique_t1:
        assert f not in shared_set


def test_compute_foundation_convergence_actions_multiple_pairs():
    """Multiple independent cross-parent pairs all trigger convergence."""
    from app.digest_metrics import compute_foundation_convergence_actions

    now = _utcnow()
    # Pair 1: two creates with shared finding
    t1a = make_task(
        created_at=now,
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "hash_A"},
    )
    t1b = make_task(
        created_at=now + timedelta(minutes=1),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "create"}],
        context={"finding_hash": "hash_A"},
    )
    # Pair 2: two fixes with shared capability
    t2a = make_task(
        created_at=now + timedelta(minutes=2),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "fix"}],
        context={"provides_capability": "auth"},
    )
    t2b = make_task(
        created_at=now + timedelta(minutes=3),
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=uuid4(),
        payloads=[{"type": "fix"}],
        context={"provides_capability": "auth"},
    )

    actions = compute_foundation_convergence_actions([t1a, t1b, t2a, t2b])

    assert len(actions) == 2
    # Verify both pairs are represented
    pair_ids = {(a["foundation_id"], a["dependent_id"]) for a in actions}
    assert (str(t1a.id), str(t1b.id)) in pair_ids
    assert (str(t2a.id), str(t2b.id)) in pair_ids


# ---------------------------------------------------------------------------
# compute_effort_model_efficiency — per-effort-tier / per-actual-model rollup
# ---------------------------------------------------------------------------


def _eff_task(
    *,
    effort=None,
    task_type="create",
    provider="anthropic",
    model="claude-opus-4-8",
    input_tokens=0,
    output_tokens=0,
    call_count=1,
    landed=False,
    status=TaskStatus.COMPLETED,
    context=None,
    attempt_history=None,
):
    """Build a terminal task for the effort/model efficiency rollup tests.

    Tokens go through ``api_call_summary`` (the current-attempt source
    ``get_task_total_llm_totals`` reads); ``landed`` stamps the canonical
    ``pr_merged`` landed signal that ``is_merged_completion`` reads.
    ``attempt_history`` (a list of banked-attempt dicts with provider/model/
    llm_input_tokens/llm_output_tokens/llm_call_count) feeds the per-attempt
    cost split that ``_task_llm_attempts`` reads.
    """
    payload = {"type": task_type}
    if effort is not None:
        payload["effort"] = effort
    ctx = dict(context or {})
    if landed:
        ctx["pr_merged"] = True
    kwargs = {}
    if attempt_history is not None:
        # attempt_history is a context-backed property; the persisted column is
        # attempt_history_db (what the SQLModel constructor accepts directly).
        kwargs["attempt_history_db"] = attempt_history
    return make_task(
        status=status,
        provider=provider,
        model=model,
        payloads=[payload],
        context=ctx,
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": call_count,
        },
        **kwargs,
    )


def test_effort_model_efficiency_aggregates_by_effort_and_model():
    """Per-effort and per-model rows carry honest run/landed/cost numbers, and
    cost is priced with the shared LLM_TOKEN_PRICING table."""
    opus_landed_cost = _priced_llm_cost(
        "anthropic", "claude-opus-4-8", 1000, 2000
    )
    opus_unlanded_cost = _priced_llm_cost(
        "anthropic", "claude-opus-4-8", 1000, 1000
    )
    sonnet_landed_cost = _priced_llm_cost(
        "anthropic", "claude-sonnet-4-6", 2000, 1000
    )
    high_cost = opus_landed_cost + opus_unlanded_cost

    tasks = [
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=2000,
            landed=True,
        ),
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=1000,
            landed=False,
        ),
        _eff_task(
            effort="medium",
            model="claude-sonnet-4-6",
            input_tokens=2000,
            output_tokens=1000,
            landed=True,
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    by_effort = {r["effort"]: r for r in result["by_effort"]}
    # Effort rows are ordered low->max: medium precedes high.
    assert [r["effort"] for r in result["by_effort"]] == ["medium", "high"]

    high = by_effort["high"]
    assert high["run_count"] == 2
    assert high["landed_count"] == 1
    assert high["landed_rate"] == 0.5
    assert high["total_cost_usd"] == pytest.approx(high_cost)
    # cost_per_merge = priced cost / landed PRs.
    assert high["cost_per_merge"] == pytest.approx(high_cost)
    assert high["input_tokens"] == 2000
    assert high["output_tokens"] == 3000

    medium = by_effort["medium"]
    assert medium["run_count"] == 1
    assert medium["landed_rate"] == 1.0
    assert medium["cost_per_merge"] == pytest.approx(sonnet_landed_cost)

    by_model = {(r["provider"], r["model"]): r for r in result["by_model"]}
    opus = by_model[("anthropic", "claude-opus-4-8")]
    assert opus["run_count"] == 2 and opus["priced"] is True
    assert opus["total_cost_usd"] == pytest.approx(high_cost)
    assert result["by_model"][0]["model"] == "claude-opus-4-8"

    totals = result["totals"]
    assert totals["run_count"] == 3
    assert totals["landed_count"] == 2
    assert totals["total_cost_usd"] == pytest.approx(high_cost + sonnet_landed_cost)
    assert totals["unpriced_coverage_fraction"] == 1.0


def test_effort_model_efficiency_cancelled_runs_add_spend_not_landings():
    """Cancelled terminal runs can burn tokens, so they must add spend/run count
    without inflating landed/merge counts."""
    completed_cost = _priced_llm_cost("anthropic", "claude-opus-4-8", 1000, 1000)
    cancelled_cost = _priced_llm_cost("anthropic", "claude-opus-4-8", 500, 500)
    expected_total = completed_cost + cancelled_cost

    tasks = [
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=1000,
            landed=True,
            status=TaskStatus.COMPLETED,
        ),
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=500,
            output_tokens=500,
            landed=False,
            status=TaskStatus.CANCELLED,
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    high = {r["effort"]: r for r in result["by_effort"]}["high"]
    assert high["run_count"] == 2
    assert high["landed_count"] == 1
    assert high["landed_rate"] == 0.5
    assert high["total_cost_usd"] == pytest.approx(expected_total)
    assert high["cost_per_merge"] == pytest.approx(expected_total)

    totals = result["totals"]
    assert totals["run_count"] == 2
    assert totals["landed_count"] == 1
    assert totals["total_cost_usd"] == pytest.approx(expected_total)


def test_effort_model_efficiency_reconciles_with_llm_cost_breakdown():
    """Total priced LLM cost AND the per-actual-model breakdown equal
    compute_llm_cost_breakdown over the same tasks — the two cost surfaces must
    not diverge, including for a task that retried on a different model."""
    from app.efficiency_snapshot import compute_llm_cost_breakdown

    tasks = [
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1500,
            output_tokens=2500,
            landed=True,
        ),
        _eff_task(
            effort="low",
            model="claude-haiku-4-5-20251001",
            input_tokens=4000,
            output_tokens=1000,
            landed=False,
        ),
        # Multi-attempt task: an opus attempt was BANKED, then it retried and
        # finished on sonnet. Each attempt must be priced at its OWN model rate,
        # so both cost surfaces must agree on the per-model split, not just total.
        _eff_task(
            effort="high",
            model="claude-sonnet-4-6",  # final/current attempt ran on sonnet
            input_tokens=600,
            output_tokens=300,
            landed=True,
            attempt_history=[
                {
                    "provider": "anthropic",
                    "model": "claude-opus-4-8",
                    "llm_input_tokens": 800,
                    "llm_output_tokens": 1200,
                    "llm_call_count": 2,
                }
            ],
        ),
    ]

    eff = compute_effort_model_efficiency(tasks)
    breakdown = compute_llm_cost_breakdown(tasks)

    assert eff["totals"]["total_cost_usd"] == pytest.approx(
        breakdown["totals"]["total_cost_usd"]
    )
    # Per-actual-model cost must reconcile too — the multi-attempt task's opus
    # tokens land in the opus bucket, its sonnet tokens in the sonnet bucket.
    eff_by_model = {
        (r["provider"], r["model"]): r["total_cost_usd"] for r in eff["by_model"]
    }
    bd_by_model = {
        (r["provider"], r["model"]): r["total_cost_usd"] for r in breakdown["by_model"]
    }
    assert eff_by_model == pytest.approx(bd_by_model)


def test_effort_model_efficiency_splits_retried_task_across_attempt_models():
    """A task that retried on a different model attributes each attempt's tokens
    and cost to that attempt's own model, while run/landed counts stay per-task
    (credited once per distinct model bucket, never once per attempt)."""
    sonnet_current_cost = _priced_llm_cost(
        "anthropic", "claude-sonnet-4-6", 2000, 1000
    )
    opus_retry_cost = _priced_llm_cost("anthropic", "claude-opus-4-8", 1000, 2000)

    tasks = [
        _eff_task(
            effort="high",
            model="claude-sonnet-4-6",  # final attempt ran on sonnet
            input_tokens=2000,
            output_tokens=1000,
            landed=True,
            attempt_history=[
                {
                    "provider": "anthropic",
                    "model": "claude-opus-4-8",  # earlier attempt ran on opus
                    "llm_input_tokens": 1000,
                    "llm_output_tokens": 2000,
                    "llm_call_count": 3,
                }
            ],
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    by_model = {(r["provider"], r["model"]): r for r in result["by_model"]}
    opus = by_model[("anthropic", "claude-opus-4-8")]
    sonnet = by_model[("anthropic", "claude-sonnet-4-6")]
    # Each attempt priced at its OWN model's rate.
    assert opus["total_cost_usd"] == pytest.approx(opus_retry_cost)
    assert opus["input_tokens"] == 1000 and opus["output_tokens"] == 2000
    assert sonnet["total_cost_usd"] == pytest.approx(sonnet_current_cost)
    assert sonnet["input_tokens"] == 2000 and sonnet["output_tokens"] == 1000
    # Run count credited ONCE per distinct model bucket, not once per attempt.
    assert opus["run_count"] == 1 and sonnet["run_count"] == 1
    assert opus["landed_count"] == 1 and sonnet["landed_count"] == 1

    # Effort bucket: one task, both attempts' cost summed; run/landed counted once.
    high = {r["effort"]: r for r in result["by_effort"]}["high"]
    assert high["run_count"] == 1
    assert high["landed_count"] == 1
    assert high["total_cost_usd"] == pytest.approx(
        opus_retry_cost + sonnet_current_cost
    )
    # resolved_models exposes both models the high tier actually ran on.
    resolved = {r["model"] for r in high["resolved_models"]}
    assert resolved == {"claude-opus-4-8", "claude-sonnet-4-6"}

    totals = result["totals"]
    assert totals["run_count"] == 1  # one task, not two attempts
    assert totals["landed_count"] == 1
    assert totals["total_cost_usd"] == pytest.approx(
        opus_retry_cost + sonnet_current_cost
    )


def test_effort_model_efficiency_cost_per_merge_uses_verified_merge():
    """Degraded (no-CI fall-open) merges count in landed_count but are excluded
    from the verified payoff figures, and cost_per_merge divides by the verified
    count so degraded merges do not make spend look cheaper."""
    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS

    degraded_reason = next(iter(DEGRADED_UNVERIFIED_REASONS))
    verified_cost = _priced_llm_cost("anthropic", "claude-opus-4-8", 1000, 2000)
    degraded_cost = _priced_llm_cost("anthropic", "claude-opus-4-8", 1000, 1000)
    total_cost = verified_cost + degraded_cost
    tasks = [
        # Verified merge: no fall-open reason.
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=2000,
            landed=True,
        ),
        # Degraded fall-open merge: landed but NOT verified.
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=1000,
            landed=True,
            context={"review_loop": {"unverified_merge_reason": degraded_reason}},
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    high = {r["effort"]: r for r in result["by_effort"]}["high"]
    # Raw landed counts BOTH merges; verified counts only the real one.
    assert high["landed_count"] == 2
    assert high["landed_rate"] == 1.0
    assert high["verified_landed_count"] == 1
    assert high["verified_landed_rate"] == 0.5
    # cost_per_merge divides total cost by VERIFIED count (1).
    assert high["total_cost_usd"] == pytest.approx(total_cost)
    assert high["cost_per_merge"] == pytest.approx(total_cost)

    by_model = {(r["provider"], r["model"]): r for r in result["by_model"]}
    opus = by_model[("anthropic", "claude-opus-4-8")]
    assert opus["landed_count"] == 2
    assert opus["verified_landed_count"] == 1
    assert opus["cost_per_merge"] == pytest.approx(total_cost)

    totals = result["totals"]
    assert totals["landed_count"] == 2
    assert totals["verified_landed_count"] == 1
    assert totals["verified_landed_rate"] == 0.5
    assert totals["cost_per_merge"] == pytest.approx(total_cost)


def test_effort_model_efficiency_unpriced_combo_not_folded_into_priced():
    """An unpriced (provider, model) combo contributes 0 cost, is counted as a
    run/token bearer, and is surfaced in unpriced_combos — never folded in."""
    priced_total = _priced_llm_cost("anthropic", "claude-opus-4-8", 1000, 1000)

    tasks = [
        _eff_task(
            effort="high",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=1000,
            landed=True,
        ),
        # Unknown model with real tokens — priceable but unpriced.
        _eff_task(
            effort="low",
            provider="anthropic",
            model="claude-mystery-9",
            input_tokens=5000,
            output_tokens=5000,
            landed=False,
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    assert "anthropic:claude-mystery-9" in result["unpriced_combos"]
    low = {r["effort"]: r for r in result["by_effort"]}["low"]
    assert low["total_cost_usd"] == 0.0
    assert low["unpriced_run_count"] == 1
    assert low["priced_run_count"] == 0
    assert low["unpriced_coverage_fraction"] == 0.0
    # The unpriced run still counts toward run/token volume.
    assert low["run_count"] == 1
    assert low["input_tokens"] == 5000
    # Totals: only the opus task's cost is folded in (the mystery model adds 0).
    assert result["totals"]["total_cost_usd"] == pytest.approx(priced_total)
    estimated_unpriced = 5000 * (
        UNPRICED_FALLBACK_INPUT_RATE + UNPRICED_FALLBACK_OUTPUT_RATE
    )
    assert result["totals"]["unpriced_coverage_fraction"] == round(
        priced_total / (priced_total + estimated_unpriced), 4
    )
    assert result["totals"]["task_coverage_fraction"] == 0.5


def test_effort_model_efficiency_surfaces_fallback_divergence():
    """A requested->effective model downgrade is surfaced both in the fallbacks
    block and as a divergent resolved_model under the requested effort tier."""
    tasks = [
        _eff_task(
            effort="high",
            model="claude-sonnet-4-6",  # actually ran on sonnet, not opus
            input_tokens=1000,
            output_tokens=1000,
            landed=True,
            context={
                "model_fallback": {
                    "requested": "claude-opus-4-8",
                    "effective": "claude-sonnet-4-6",
                    "provider": "anthropic",
                    "reason": "no_account_with_opus_access",
                }
            },
        ),
    ]

    result = compute_effort_model_efficiency(tasks)

    assert result["fallbacks"]["total"] == 1
    combo = result["fallbacks"]["combos"][0]
    assert combo["requested"] == "claude-opus-4-8"
    assert combo["effective"] == "claude-sonnet-4-6"
    assert combo["count"] == 1

    high = {r["effort"]: r for r in result["by_effort"]}["high"]
    resolved = high["resolved_models"]
    assert len(resolved) == 1
    # High effort was requested but the run resolved onto sonnet — visible divergence.
    assert resolved[0]["model"] == "claude-sonnet-4-6"
    assert resolved[0]["run_count"] == 1


def test_effort_model_efficiency_missing_effort_resolves_to_default_tier():
    """A task with no explicit payload effort and no routing config resolves to
    the dispatcher's DEFAULT_EFFORT_LEVEL ('medium'), not 'unspecified' — its
    effective routing effort is what selected the model and drove cost."""
    from app.max_plan_pool import DEFAULT_EFFORT_LEVEL

    tasks = [
        _eff_task(
            effort=None,
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=100,
            landed=True,
        ),
    ]
    # Empty runtime_config -> no payload/context/type/category effort -> default.
    result = compute_effort_model_efficiency(tasks, runtime_config={})
    efforts = {r["effort"] for r in result["by_effort"]}
    assert efforts == {DEFAULT_EFFORT_LEVEL}


def test_effort_model_efficiency_buckets_by_resolved_routing_effort():
    """Tasks with NO explicit payload effort (review/strategic_review/ops types)
    land in their CATEGORY routing tier — the effort that actually selected the
    model — instead of collapsing into 'unspecified'."""
    runtime_config = {
        "task_type_categories": {
            "review": ["review", "strategic_review"],
            "strategic": ["strategic"],
            "ops": ["deps"],
        },
        "category_routing": {
            "review": {"effort": "high"},
            "strategic": {"effort": "max"},
            "ops": {"effort": "low"},
            "_default": {"effort": "medium"},
        },
    }
    tasks = [
        _eff_task(
            effort=None,
            task_type="review",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=100,
            landed=False,
        ),
        _eff_task(
            effort=None,
            task_type="strategic_review",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=100,
            landed=False,
        ),
        _eff_task(
            effort=None,
            task_type="strategic",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=100,
            landed=False,
        ),
        _eff_task(
            effort=None,
            task_type="deps",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=100,
            landed=False,
        ),
    ]
    result = compute_effort_model_efficiency(tasks, runtime_config=runtime_config)
    by_effort = {r["effort"]: r for r in result["by_effort"]}
    assert "unspecified" not in by_effort
    # review + strategic_review both resolve to the 'review' category -> high.
    assert by_effort["high"]["run_count"] == 2
    assert by_effort["max"]["run_count"] == 1
    assert by_effort["low"]["run_count"] == 1


def test_effort_model_efficiency_explicit_payload_effort_wins():
    """Explicit payload effort is the most-specific layer and overrides the
    category routing config — so previously-bucketed tasks are unaffected."""
    runtime_config = {
        "task_type_categories": {"review": ["review"]},
        "category_routing": {"review": {"effort": "high"}},
    }
    tasks = [
        # type=review would resolve to 'high' by category, but explicit 'low'
        # in the payload wins.
        _eff_task(
            effort="low",
            task_type="review",
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=100,
            landed=False,
        ),
    ]
    result = compute_effort_model_efficiency(tasks, runtime_config=runtime_config)
    efforts = {r["effort"] for r in result["by_effort"]}
    assert efforts == {"low"}


def test_effort_model_efficiency_empty_input():
    """Empty input yields empty rows and zeroed totals, not an error."""
    result = compute_effort_model_efficiency([])
    assert result["by_effort"] == []
    assert result["by_model"] == []
    assert result["unpriced_combos"] == []
    assert result["fallbacks"]["total"] == 0
    assert result["totals"]["run_count"] == 0
    assert result["totals"]["landed_rate"] is None
    assert result["totals"]["cost_per_merge"] is None


def test_provider_routing_share_by_provider():
    """Per-provider routing share correctly aggregates by_model rows.

    Exercises:
      - mixed DeepSeek + Anthropic tasks with different volumes
      - all-DeepSeek (no Anthropic) works without error
      - empty input yields empty by_provider list and zero totals
    """
    import uuid

    from app.models import TaskStatus

    project_id = uuid.uuid4()

    # -- Mixed providers ---------------------------------------------------
    mixed_tasks = (
        [make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider="deepseek",
            model="deepseek-v4-flash",
            api_call_summary={
                "total_input_tokens": 1000, "total_output_tokens": 500,
                "llm_call_count": 5,
            },
        ) for _ in range(30)]
        + [make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_call_summary={
                "total_input_tokens": 1000, "total_output_tokens": 500,
                "llm_call_count": 5,
            },
        ) for _ in range(10)]
    )
    by_model = compute_effort_model_efficiency(mixed_tasks)["by_model"]
    share = compute_provider_routing_share(by_model)

    assert "by_provider" in share
    assert "total_run_count" in share
    assert "total_pr_eligible_run_count" in share

    # Both providers present.
    prov_map = {p["provider"]: p for p in share["by_provider"]}
    assert "deepseek" in prov_map
    assert "anthropic" in prov_map
    assert share["total_run_count"] == 40
    assert share["total_pr_eligible_run_count"] == 40

    # DeepSeek share: 30/40 = 75%.
    ds = prov_map["deepseek"]
    assert ds["run_count"] == 30
    an = prov_map["anthropic"]
    assert an["run_count"] == 10
    # All merged → verified_landed_rate should be 1.0.
    assert an["verified_landed_rate"] == 1.0, (
        f"expected all Anthropic tasks landed, got {an['verified_landed_rate']}"
    )

    # -- All DeepSeek (no Anthropic) ---------------------------------------
    ds_only_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
            payloads=[{"type": "create", "effort": "low"}],
            provider="deepseek",
            model="deepseek-v4-flash",
            api_call_summary={
                "total_input_tokens": 1000, "total_output_tokens": 500,
                "llm_call_count": 5,
            },
        ) for _ in range(15)
    ]
    by_model_ds = compute_effort_model_efficiency(ds_only_tasks)["by_model"]
    share_ds = compute_provider_routing_share(by_model_ds)
    prov_map_ds = {p["provider"]: p for p in share_ds["by_provider"]}
    assert "deepseek" in prov_map_ds
    assert "anthropic" not in prov_map_ds
    assert share_ds["total_run_count"] == 15
    assert prov_map_ds["deepseek"]["run_count"] == 15

    # -- Empty input -------------------------------------------------------
    share_empty = compute_provider_routing_share([])
    assert share_empty["by_provider"] == []
    assert share_empty["total_run_count"] == 0
    assert share_empty["total_pr_eligible_run_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# _compute_first_attempt_breakdowns: verified_overall
# ─────────────────────────────────────────────────────────────────────────────


def _fa_task(*, succeeded: bool, reason: str | None = None) -> object:
    """Create a minimal in-memory task-like object for first-attempt breakdown tests."""
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY, LANDED_SIGNAL_KEY

    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if reason is not None:
        ctx[LANDED_SIGNAL_KEY] = True
        ctx[LANDED_ON_INTEGRATION_KEY] = True
        ctx["review_loop"] = {"unverified_merge_reason": reason}
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_verified_overall_excludes_degraded():
    """verified_overall must not count degraded-unverified merges as successes."""
    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS

    # One verified success, one degraded-unverified "success" (should be excluded).
    degraded_reason = next(iter(DEGRADED_UNVERIFIED_REASONS))
    tasks = [
        _fa_task(succeeded=True, reason=None),  # verified success
        _fa_task(succeeded=True, reason=degraded_reason),  # degraded — exclude
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    # overall counts both True flags → 2/2 = 1.0
    assert result["overall"] == 1.0
    # verified_overall counts only the verified success → 1/2 = 0.5
    assert result["verified_overall"] == 0.5


def test_first_attempt_breakdowns_verified_overall_benign_counts():
    """Benign fall-opens must still count in verified_overall."""
    from app.ci_resolution import BENIGN_UNVERIFIED_REASONS

    benign_reason = next(iter(BENIGN_UNVERIFIED_REASONS))
    tasks = [
        _fa_task(succeeded=True, reason=None),  # verified
        _fa_task(succeeded=True, reason=benign_reason),  # benign — should count
        _fa_task(succeeded=False, reason=None),  # plain failure
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    # overall: 2 successes / 3 measured = 0.67
    assert result["overall"] == round(2 / 3, 2)
    # verified_overall: both successes count (benign is fine) → 2/3 = 0.67
    assert result["verified_overall"] == round(2 / 3, 2)


def test_first_attempt_breakdowns_verified_overall_all_degraded():
    """verified_overall is 0.0 when all successes are degraded-unverified."""
    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS

    degraded_reason = next(iter(DEGRADED_UNVERIFIED_REASONS))
    tasks = [
        _fa_task(succeeded=True, reason=degraded_reason),
        _fa_task(succeeded=True, reason=degraded_reason),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["overall"] == 1.0
    assert result["verified_overall"] == 0.0


def test_first_attempt_breakdowns_verified_overall_no_measured_is_none():
    """verified_overall returns None when no task has the metric recorded."""
    tasks = [
        make_task(
            status=TaskStatus.COMPLETED, payloads=[{"type": "create"}], context={}
        )
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["verified_overall"] is None


def test_first_attempt_breakdowns_playbook_lift():
    """playbook and arch-context lifts are named, guarded deltas with trends."""

    def mk(s, pb=None, arch=None):
        return make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "_first_attempt_succeeded": s,
                **({"_had_playbook": pb} if pb is not None else {}),
                **({"_had_arch_context": arch} if arch is not None else {}),
            },
        )

    def tasks(spec):
        return [mk(s, **flags) for s, count, flags in spec for _ in range(count)]

    result = _compute_first_attempt_breakdowns(
        tasks(
            [
                (True, 8, {"pb": True}),
                (False, 2, {"pb": True}),
                (True, 5, {"pb": False}),
                (False, 5, {"pb": False}),
                (True, 7, {"arch": True}),
                (False, 3, {"arch": True}),
                (True, 4, {"arch": False}),
                (False, 6, {"arch": False}),
            ]
        ),
        prev_terminal_tasks=tasks(
            [
                (True, 6, {"pb": True}),
                (False, 4, {"pb": True}),
                (True, 5, {"pb": False}),
                (False, 5, {"pb": False}),
                (True, 5, {"arch": True}),
                (False, 5, {"arch": True}),
                (True, 4, {"arch": False}),
                (False, 6, {"arch": False}),
            ]
        ),
    )
    assert result["playbook_lift"] == 0.3
    assert result["playbook_lift_trend"] == "improving"
    assert result["arch_context_lift"] == 0.3
    assert result["arch_context_lift_trend"] == "improving"
    assert (
        _compute_first_attempt_breakdowns(
            [mk(True, pb=True) for _ in range(9)]
            + [mk(False, pb=False) for _ in range(9)]
            + [mk(True, arch=True) for _ in range(10)]
            + [mk(False, arch=False) for _ in range(9)]
        )["playbook_lift"]
        is None
    )
    assert (
        _compute_first_attempt_breakdowns(
            [mk(True, pb=True) for _ in range(9)]
            + [mk(False, pb=False) for _ in range(9)]
            + [mk(True, arch=True) for _ in range(10)]
            + [mk(False, arch=False) for _ in range(9)]
        )["arch_context_lift"]
        is None
    )


# ── _compute_first_attempt_breakdowns: repo-map breakdowns ──────────────────


def _fa_rm_task(*, succeeded: bool, had_repo_map: bool | None) -> object:
    """Task fixture for repo-map breakdown tests."""
    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if had_repo_map is not None:
        ctx["_had_repo_map"] = had_repo_map
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_with_without_repo_map_rates():
    """with_repo_map and without_repo_map rates are computed from _had_repo_map flag."""
    tasks = [
        _fa_rm_task(succeeded=True, had_repo_map=True),
        _fa_rm_task(succeeded=True, had_repo_map=True),
        _fa_rm_task(succeeded=False, had_repo_map=False),
        _fa_rm_task(succeeded=False, had_repo_map=False),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_repo_map"] == 1.0
    assert result["without_repo_map"] == 0.0
    # Both arms have < 5 tasks, so lift should be None
    assert result["repo_map_lift"] is None


def test_first_attempt_breakdowns_repo_map_lift_computed_with_enough_samples():
    """repo_map_lift is the difference when both arms have >= 5 samples."""
    tasks = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 5 + [
        _fa_rm_task(succeeded=False, had_repo_map=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_repo_map"] == 1.0
    assert result["without_repo_map"] == 0.0
    assert result["repo_map_lift"] == 1.0


def test_first_attempt_breakdowns_repo_map_lift_none_when_one_arm_empty():
    """repo_map_lift is None when one arm has no tasks."""
    tasks = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 6
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_repo_map"] == 1.0
    assert result["without_repo_map"] is None
    assert result["repo_map_lift"] is None


def test_first_attempt_breakdowns_repo_map_no_flag_not_counted():
    """Tasks without _had_repo_map do not appear in either arm."""
    tasks = [
        _fa_rm_task(succeeded=True, had_repo_map=None),
        _fa_rm_task(succeeded=False, had_repo_map=None),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_repo_map"] is None
    assert result["without_repo_map"] is None
    assert result["repo_map_lift"] is None


def _fa_section_task(
    *, succeeded: bool, flag_key: str, flag_val: bool | None
) -> object:
    """Task fixture for prompt-section breakdown tests."""
    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if flag_val is not None:
        ctx[flag_key] = flag_val
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_module_interfaces_rates():
    """with/without_module_interfaces rates computed from _had_module_interfaces flag."""
    tasks = [
        _fa_section_task(
            succeeded=True, flag_key="_had_module_interfaces", flag_val=True
        ),
        _fa_section_task(
            succeeded=True, flag_key="_had_module_interfaces", flag_val=True
        ),
        _fa_section_task(
            succeeded=False, flag_key="_had_module_interfaces", flag_val=False
        ),
        _fa_section_task(
            succeeded=False, flag_key="_had_module_interfaces", flag_val=False
        ),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_module_interfaces"] == 1.0
    assert result["without_module_interfaces"] == 0.0


def test_first_attempt_breakdowns_related_tests_rates():
    """with/without_related_tests rates computed from _had_related_tests flag."""
    tasks = [
        _fa_section_task(succeeded=True, flag_key="_had_related_tests", flag_val=True),
        _fa_section_task(
            succeeded=False, flag_key="_had_related_tests", flag_val=False
        ),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_related_tests"] == 1.0
    assert result["without_related_tests"] == 0.0


def test_first_attempt_breakdowns_affected_consumers_rates():
    """with/without_affected_consumers rates computed from _had_affected_consumers flag."""
    tasks = [
        _fa_section_task(
            succeeded=True, flag_key="_had_affected_consumers", flag_val=True
        ),
        _fa_section_task(
            succeeded=False, flag_key="_had_affected_consumers", flag_val=False
        ),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_affected_consumers"] == 1.0
    assert result["without_affected_consumers"] == 0.0


def test_first_attempt_breakdowns_section_flags_no_flag_not_counted():
    """Tasks without section flags do not appear in either arm."""
    tasks = [
        _fa_section_task(
            succeeded=True, flag_key="_had_module_interfaces", flag_val=None
        ),
        _fa_section_task(succeeded=False, flag_key="_had_related_tests", flag_val=None),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["with_module_interfaces"] is None
    assert result["without_module_interfaces"] is None
    assert result["with_related_tests"] is None
    assert result["without_related_tests"] is None


def test_first_attempt_breakdowns_repo_map_lift_computed_with_enough_samples_via_definitions():
    """repo_map_lift is computed via the definition-driven path when both arms have >= 5 samples."""
    tasks = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 5 + [
        _fa_rm_task(succeeded=False, had_repo_map=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(tasks)
    assert result["trial_breakdowns"]["repo_map"]["lift"] == 1.0
    assert result["trial_breakdowns"]["repo_map"]["with_n"] == 5
    assert result["trial_breakdowns"]["repo_map"]["without_n"] == 5
    # Flat alias keys remain for backward compatibility.
    assert result["repo_map_lift"] == 1.0
    assert result["with_repo_map_n"] == 5
    assert result["without_repo_map_n"] == 5


def test_first_attempt_breakdowns_trial_breakdowns_key_present():
    """trial_breakdowns sub-dict is present and contains per-cap keys."""
    tasks = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 5 + [
        _fa_rm_task(succeeded=False, had_repo_map=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(tasks)
    assert "trial_breakdowns" in result
    bd = result["trial_breakdowns"]["repo_map"]
    assert "lift" in bd
    assert "with_n" in bd
    assert "without_n" in bd
    assert "with_rate" in bd
    assert "without_rate" in bd


def test_first_attempt_breakdowns_legacy_keys_still_present():
    """Flat legacy keys repo_map_lift / with_repo_map_n / without_repo_map_n remain."""
    tasks = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 3
    result = _compute_first_attempt_breakdowns(tasks)
    assert "repo_map_lift" in result
    assert "with_repo_map_n" in result
    assert "without_repo_map_n" in result
    assert "with_repo_map" in result
    assert "without_repo_map" in result


def _fa_test_cap_task(*, succeeded: bool, had_test_cap: bool | None) -> object:
    """Task fixture for synthetic test_cap trial definition tests."""
    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if had_test_cap is not None:
        ctx["_had_test_cap"] = had_test_cap
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_second_trial_definition_independent():
    """A second trial definition produces its own lift keys without affecting repo_map."""
    _test_cap_def = {
        "cap_name": "test_cap",
        "context_key": "_had_test_cap",
        "thresholds": {"min_n_per_arm": 30, "adopt_lift": 0.05, "reject_lift": -0.03},
    }
    tasks = (
        [_fa_rm_task(succeeded=True, had_repo_map=True)] * 5
        + [_fa_rm_task(succeeded=False, had_repo_map=False)] * 5
        + [_fa_test_cap_task(succeeded=True, had_test_cap=True)] * 5
        + [_fa_test_cap_task(succeeded=False, had_test_cap=False)] * 5
    )
    from app.capability_trial_verdict import get_trial_definitions

    combined_defs = get_trial_definitions() + [_test_cap_def]
    result = _compute_first_attempt_breakdowns(tasks, trial_definitions=combined_defs)
    # repo_map arm still correct
    assert result["trial_breakdowns"]["repo_map"]["lift"] == 1.0
    # test_cap arm independently computed — lift is None because < 5 pure test_cap tasks
    # in each arm (mixed-context tasks don't contribute to both arms)
    assert "test_cap" in result["trial_breakdowns"]
    # Flat keys generated for test_cap
    assert "test_cap_lift" in result
    assert "with_test_cap_n" in result
    assert "without_test_cap_n" in result


def test_first_attempt_breakdowns_existing_playbook_keys_unaffected():
    """Adding repo-map keys does not remove or alter the existing playbook keys."""
    tasks = [
        make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": True, "_had_playbook": True},
        ),
        make_task(
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"_first_attempt_succeeded": False, "_had_playbook": False},
        ),
    ]
    result = _compute_first_attempt_breakdowns(tasks)
    assert "with_playbook" in result
    assert "without_playbook" in result
    assert result["with_playbook"] == 1.0
    assert result["without_playbook"] == 0.0
    # New keys present
    assert "with_repo_map" in result
    assert "without_repo_map" in result
    assert "repo_map_lift" in result
    assert "with_module_interfaces" in result
    assert "without_module_interfaces" in result
    assert "with_related_tests" in result
    assert "without_related_tests" in result
    assert "with_affected_consumers" in result
    assert "without_affected_consumers" in result


# ── _compute_first_attempt_breakdowns: repo-map assignment (intent-to-treat) ─


def _fa_rm_arm_task(
    *, succeeded: bool, repo_map_arm: bool | None, had_repo_map: bool | None
) -> object:
    """Task fixture for repo-map assignment-vs-receipt tests."""
    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if repo_map_arm is not None:
        ctx["_repo_map_arm"] = repo_map_arm
    if had_repo_map is not None:
        ctx["_had_repo_map"] = had_repo_map
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_repo_map_assignment_not_receipt():
    """Intent-to-treat: treatment arm uses _repo_map_arm assignment, not _had_repo_map receipt.

    A task assigned to treatment (_repo_map_arm=True) but with no actual map
    (_had_repo_map=False) counts in the treatment arm for lift purposes.
    """
    # 5 treatment-assigned tasks: all succeeded, but only 4 actually got the map
    treatment_tasks = [
        _fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=True)
    ] * 4 + [_fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=False)] * 1
    # 5 holdback-assigned tasks: none succeeded
    holdback_tasks = [
        _fa_rm_arm_task(succeeded=False, repo_map_arm=False, had_repo_map=False)
    ] * 5
    tasks = treatment_tasks + holdback_tasks
    result = _compute_first_attempt_breakdowns(tasks)
    # Treatment arm should have 5 (assigned), not 4 (received)
    assert result["trial_breakdowns"]["repo_map"]["with_n"] == 5
    assert result["trial_breakdowns"]["repo_map"]["without_n"] == 5
    assert result["trial_breakdowns"]["repo_map"]["with_rate"] == 1.0
    assert result["trial_breakdowns"]["repo_map"]["without_rate"] == 0.0
    assert result["repo_map_lift"] == 1.0
    # Flat alias keys preserved
    assert result["with_repo_map_n"] == 5
    assert result["without_repo_map_n"] == 5


def test_first_attempt_breakdowns_repo_map_legacy_fallback():
    """Legacy tasks without _repo_map_arm fall back to _had_repo_map for arm assignment."""
    legacy_with = [_fa_rm_task(succeeded=True, had_repo_map=True)] * 5
    legacy_without = [_fa_rm_task(succeeded=False, had_repo_map=False)] * 5
    result = _compute_first_attempt_breakdowns(legacy_with + legacy_without)
    assert result["trial_breakdowns"]["repo_map"]["with_n"] == 5
    assert result["trial_breakdowns"]["repo_map"]["without_n"] == 5
    assert result["repo_map_lift"] == 1.0


def test_first_attempt_breakdowns_repo_map_assignment_overrides_receipt():
    """When _repo_map_arm is present it wins over _had_repo_map for arm assignment."""
    # Task assigned to holdback (_repo_map_arm=False) but had a map anyway
    # (could happen if the trial was reconfigured mid-run). Should count as holdback.
    holdback_with_map = _fa_rm_arm_task(
        succeeded=True, repo_map_arm=False, had_repo_map=True
    )
    # 5 treatment tasks + 5 holdback tasks to pass the min-sample gate
    treatment_tasks = [
        _fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=True)
    ] * 5
    holdback_tasks = [holdback_with_map] + [
        _fa_rm_arm_task(succeeded=False, repo_map_arm=False, had_repo_map=False)
    ] * 4
    result = _compute_first_attempt_breakdowns(treatment_tasks + holdback_tasks)
    assert result["trial_breakdowns"]["repo_map"]["with_n"] == 5
    assert result["trial_breakdowns"]["repo_map"]["without_n"] == 5


def test_first_attempt_breakdowns_repo_map_compliance_subdict_present():
    """compliance sub-dict is present in trial_breakdowns['repo_map'] when assignment key differs."""
    tasks = (
        [_fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=True)] * 4
        + [_fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=False)] * 1
        + [_fa_rm_arm_task(succeeded=False, repo_map_arm=False, had_repo_map=False)] * 5
    )
    result = _compute_first_attempt_breakdowns(tasks)
    bd = result["trial_breakdowns"]["repo_map"]
    assert "compliance" in bd
    # 4 of 5 treatment tasks had the map → 80%
    assert bd["compliance"]["with_rate"] == 0.8
    # 1 of 5 treatment tasks did NOT have the map → 20%
    assert bd["compliance"]["without_rate"] == 0.2


def test_first_attempt_breakdowns_repo_map_compliance_all_compliant():
    """compliance.with_rate is 1.0 when all treatment-arm tasks received the map."""
    tasks = [
        _fa_rm_arm_task(succeeded=True, repo_map_arm=True, had_repo_map=True)
    ] * 5 + [
        _fa_rm_arm_task(succeeded=False, repo_map_arm=False, had_repo_map=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(tasks)
    bd = result["trial_breakdowns"]["repo_map"]
    assert bd["compliance"]["with_rate"] == 1.0
    assert bd["compliance"]["without_rate"] == 0.0


# ── _compute_first_attempt_breakdowns: diagnostics_after_edit arm (intent-to-treat) ─


def _fa_dae_arm_task(
    *, succeeded: bool, dae_arm: bool | None, had_dae: bool | None
) -> object:
    """Task fixture for diagnostics_after_edit assignment-vs-receipt tests."""
    ctx: dict = {"_first_attempt_succeeded": succeeded}
    if dae_arm is not None:
        ctx["_diagnostics_after_edit_arm"] = dae_arm
    if had_dae is not None:
        ctx["_had_diagnostics_after_edit"] = had_dae
    return make_task(
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
        context=ctx,
    )


def test_first_attempt_breakdowns_diagnostics_after_edit_arm_partitioning():
    """Intent-to-treat: arm uses _diagnostics_after_edit_arm assignment, not _had_diagnostics_after_edit receipt."""
    # 5 treatment-assigned (all succeeded), 5 holdback-assigned (none succeeded)
    treatment = [_fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=True)] * 4 + [
        _fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=False)
    ] * 1
    holdback = [_fa_dae_arm_task(succeeded=False, dae_arm=False, had_dae=False)] * 5
    result = _compute_first_attempt_breakdowns(treatment + holdback)
    bd = result["trial_breakdowns"]["diagnostics_after_edit"]
    # Partitioned by assignment key, not receipt key
    assert bd["with_n"] == 5
    assert bd["without_n"] == 5
    assert bd["with_rate"] == 1.0
    assert bd["without_rate"] == 0.0
    assert bd["lift"] == 1.0


def test_first_attempt_breakdowns_diagnostics_after_edit_legacy_fallback():
    """Legacy tasks without _diagnostics_after_edit_arm fall back to _had_diagnostics_after_edit."""
    legacy_with = [_fa_dae_arm_task(succeeded=True, dae_arm=None, had_dae=True)] * 5
    legacy_without = [
        _fa_dae_arm_task(succeeded=False, dae_arm=None, had_dae=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(legacy_with + legacy_without)
    bd = result["trial_breakdowns"]["diagnostics_after_edit"]
    assert bd["with_n"] == 5
    assert bd["without_n"] == 5
    assert bd["lift"] == 1.0


def test_first_attempt_breakdowns_diagnostics_after_edit_assignment_overrides_receipt():
    """When _diagnostics_after_edit_arm is present it wins over _had_diagnostics_after_edit."""
    # Assigned to holdback but receipt=True: must land in holdback arm
    holdback_with_receipt = _fa_dae_arm_task(
        succeeded=True, dae_arm=False, had_dae=True
    )
    treatment = [_fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=True)] * 5
    holdback = [holdback_with_receipt] + [
        _fa_dae_arm_task(succeeded=False, dae_arm=False, had_dae=False)
    ] * 4
    result = _compute_first_attempt_breakdowns(treatment + holdback)
    bd = result["trial_breakdowns"]["diagnostics_after_edit"]
    assert bd["with_n"] == 5
    assert bd["without_n"] == 5


def test_first_attempt_breakdowns_diagnostics_after_edit_compliance_subdict_present():
    """compliance sub-dict is present for diagnostics_after_edit (assignment key differs from context key)."""
    tasks = (
        [_fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=True)] * 4
        + [_fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=False)] * 1
        + [_fa_dae_arm_task(succeeded=False, dae_arm=False, had_dae=False)] * 5
    )
    result = _compute_first_attempt_breakdowns(tasks)
    bd = result["trial_breakdowns"]["diagnostics_after_edit"]
    assert "compliance" in bd
    # 4 of 5 treatment tasks actually received the capability → 80%
    assert bd["compliance"]["with_rate"] == 0.8
    assert bd["compliance"]["without_rate"] == 0.2


def test_first_attempt_breakdowns_diagnostics_after_edit_flat_alias_keys_present():
    """diagnostics_after_edit flat alias keys are generated at the top level."""
    tasks = [_fa_dae_arm_task(succeeded=True, dae_arm=True, had_dae=True)] * 5 + [
        _fa_dae_arm_task(succeeded=False, dae_arm=False, had_dae=False)
    ] * 5
    result = _compute_first_attempt_breakdowns(tasks)
    assert "diagnostics_after_edit_lift" in result
    assert "with_diagnostics_after_edit_n" in result
    assert "without_diagnostics_after_edit_n" in result
    assert result["with_diagnostics_after_edit_n"] == 5
    assert result["without_diagnostics_after_edit_n"] == 5


def test_evaluate_trial_verdict_purity_gate_blocks_adopt_when_assigned_arm_low_but_receipt_inflated():
    """Purity gate blocks adopt when assigned-arm counts are low even though receipt-inflated totals meet threshold.

    Mirrors the TestArmComposition.test_purity_gate_blocks_adopt_when_assigned_with_arm_insufficient
    pattern but framed for the diagnostics_after_edit trial context.
    """
    from app.capability_trial_verdict import evaluate_trial_verdict

    # n_with=40 / n_without=40 would ordinarily meet min_n_per_arm=30,
    # but almost all tasks got there via receipt fallback, not assignment.
    arm_comp = {
        "with_assigned": 5,
        "with_receipt": 35,
        "without_assigned": 5,
        "without_receipt": 35,
    }
    entry = evaluate_trial_verdict(
        lift=0.10,
        n_with=40,
        n_without=40,
        arm_composition=arm_comp,
    )
    assert entry["verdict"] == "continue-trial"
    assert entry["verdict_reason"] == "insufficient_assigned_arm_data"
    assert entry["arm_composition"] == arm_comp


# ── Migration-serialization slot contention ─────────────────────────────────


def _mk_migration_task(project_id, status, **kwargs):
    """A migration-producing task (migrate type) for slot-contention tests."""
    return make_task(
        project_id=project_id,
        status=status,
        payloads=[{"type": "migrate"}],
        **kwargs,
    )


def test_migration_slot_contention_holder_with_waiters():
    """A holder past the threshold with waiters reports hold age, waiter count,
    longest wait, and flips threshold_exceeded."""
    pid = uuid4()
    now = _utcnow()
    holder = _mk_migration_task(
        pid,
        TaskStatus.IN_PROGRESS,
        title="holding-migration",
        started_at=now - timedelta(seconds=_MIGRATION_SLOT_HOLD_ALERT_SECONDS + 600),
    )
    w1 = _mk_migration_task(
        pid, TaskStatus.QUEUED, updated_at=now - timedelta(minutes=30)
    )
    w2 = _mk_migration_task(
        pid, TaskStatus.BLOCKED, updated_at=now - timedelta(minutes=90)
    )
    result = compute_migration_slot_contention_summary([holder], [w1, w2])

    assert result["slot_held"] is True
    assert result["holder_task_id"] == str(holder.id)
    assert result["holder_title"] == "holding-migration"
    assert result["waiting_count"] == 2
    # longest wait is the BLOCKED task at ~90m.
    assert result["longest_wait_seconds"] >= 90 * 60 - 5
    assert result["holder_held_seconds"] >= _MIGRATION_SLOT_HOLD_ALERT_SECONDS
    assert result["threshold_exceeded"] is True
    assert len(result["projects"]) == 1
    proj = result["projects"][0]
    assert proj["project_id"] == str(pid)
    assert proj["holder_count"] == 1
    assert proj["threshold_exceeded"] is True


def test_migration_slot_contention_no_holder():
    """No IN_PROGRESS migration task -> no holder, no contention signal."""
    result = compute_migration_slot_contention_summary([], [])
    assert result["slot_held"] is False
    assert result["holder_task_id"] is None
    assert result["waiting_count"] == 0
    assert result["threshold_exceeded"] is False
    assert result["projects"] == []


def test_migration_slot_contention_held_briefly_no_threshold():
    """A recent hold with waiters is NOT threshold_exceeded; a long hold with NO
    waiters is also NOT threshold_exceeded (no one is parked)."""
    pid = uuid4()
    now = _utcnow()
    # Recently-started holder + a waiter: contended but under threshold.
    fresh_holder = _mk_migration_task(
        pid, TaskStatus.IN_PROGRESS, started_at=now - timedelta(minutes=5)
    )
    waiter = _mk_migration_task(
        pid, TaskStatus.QUEUED, updated_at=now - timedelta(minutes=3)
    )
    result = compute_migration_slot_contention_summary([fresh_holder], [waiter])
    assert result["slot_held"] is True
    assert result["waiting_count"] == 1
    assert result["threshold_exceeded"] is False

    # Long-held solo holder, no waiters: not a contention/threshold event.
    pid2 = uuid4()
    long_solo = _mk_migration_task(
        pid2,
        TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(seconds=_MIGRATION_SLOT_HOLD_ALERT_SECONDS * 3),
    )
    result2 = compute_migration_slot_contention_summary([long_solo], [])
    assert result2["slot_held"] is True
    assert result2["waiting_count"] == 0
    assert result2["threshold_exceeded"] is False


def test_migration_slot_contention_per_project_grouping():
    """Holders/waiters are grouped per project; aggregate uses the longest-held
    holder as the representative and sums waiters across projects."""
    pa, pb = uuid4(), uuid4()
    now = _utcnow()
    holder_a = _mk_migration_task(
        pa,
        TaskStatus.IN_PROGRESS,
        title="a-holder",
        started_at=now - timedelta(seconds=_MIGRATION_SLOT_HOLD_ALERT_SECONDS + 1000),
    )
    waiter_a = _mk_migration_task(
        pa, TaskStatus.QUEUED, updated_at=now - timedelta(minutes=20)
    )
    holder_b = _mk_migration_task(
        pb,
        TaskStatus.IN_PROGRESS,
        title="b-holder",
        started_at=now - timedelta(minutes=10),
    )
    result = compute_migration_slot_contention_summary([holder_a, holder_b], [waiter_a])
    assert len(result["projects"]) == 2
    by_pid = {p["project_id"]: p for p in result["projects"]}
    assert by_pid[str(pa)]["waiting_count"] == 1
    assert by_pid[str(pa)]["threshold_exceeded"] is True
    assert by_pid[str(pb)]["waiting_count"] == 0
    assert by_pid[str(pb)]["threshold_exceeded"] is False
    # Aggregate: representative holder is project A (longest held + breaching).
    assert result["holder_task_id"] == str(holder_a.id)
    assert result["waiting_count"] == 1
    assert result["threshold_exceeded"] is True


@pytest.mark.asyncio
async def test_migration_slot_contention_query_filters_and_scopes(db_session):
    """The DB-backed query counts ONLY migration-producing tasks (a non-migration
    QUEUED task is excluded) and scopes to the given project."""
    project = make_project(name="Slot contention query", user_id="user1")
    other = make_project(name="Other project", user_id="user1")
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    now = _utcnow()
    holder = _mk_migration_task(
        project.id,
        TaskStatus.IN_PROGRESS,
        title="mig-holder",
        started_at=now - timedelta(seconds=_MIGRATION_SLOT_HOLD_ALERT_SECONDS + 300),
    )
    # Migration-producing waiter (related_files under alembic/versions).
    mig_waiter = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        title="mig-waiter",
        payloads=[{"type": "create"}],
        context={"related_files": ["backend/alembic/versions/0200_x.py"]},
        updated_at=now - timedelta(minutes=40),
    )
    # Non-migration QUEUED task — must NOT be counted as a waiter.
    plain_waiter = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        title="plain-waiter",
        payloads=[{"type": "create"}],
        context={"related_files": ["admin-ui/src/App.tsx"]},
    )
    # A migration task in another project — excluded by scoping.
    other_holder = _mk_migration_task(
        other.id, TaskStatus.IN_PROGRESS, started_at=now - timedelta(hours=5)
    )
    db_session.add_all([holder, mig_waiter, plain_waiter, other_holder])
    await db_session.commit()

    result = await compute_migration_slot_contention(db_session, project_id=project.id)
    assert result["slot_held"] is True
    assert result["holder_task_id"] == str(holder.id)
    assert result["waiting_count"] == 1  # plain_waiter excluded
    assert result["threshold_exceeded"] is True
    assert len(result["projects"]) == 1
    assert result["projects"][0]["project_id"] == str(project.id)


# ── compute_format_compliance_metrics ──────────────────────────────────────


def _fc_task(task_type, summary, status=TaskStatus.COMPLETED):
    """Build a terminal task carrying a format_compliance_summary (or None)."""
    return make_task(
        status=status,
        payloads=[{"type": task_type}],
        format_compliance_summary=summary,
    )


def test_format_compliance_metrics_malformed_then_repaired_run():
    """A malformed-then-repaired run is non-compliant (0.0) but its repair counts
    as a success (1.0); a clean run is well-formed; a NULL-summary task is excluded."""
    tasks = [
        # create #1: one unparseable final output, repaired successfully on re-ask.
        _fc_task(
            "create",
            {
                "format_miss_by_reason": {"unbalanced_json": 1},
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
            },
        ),
        # create #2: clean run — no format error, no repair needed.
        _fc_task(
            "create",
            {
                "format_miss_by_reason": {},
                "format_error_total": 0,
                "format_repair_attempts": 0,
                "format_repair_successes": 0,
            },
        ),
        # fix: two misses, only one repair succeeded.
        _fc_task(
            "fix",
            {
                "format_miss_by_reason": {
                    "missing_required_key": 1,
                    "no_json_start": 1,
                },
                "format_error_total": 2,
                "format_repair_attempts": 2,
                "format_repair_successes": 1,
            },
            status=TaskStatus.FAILED,
        ),
        # NULL summary (older agent) — excluded entirely from the measure.
        _fc_task("create", None),
    ]

    result = compute_format_compliance_metrics(tasks)
    overall = result["overall"]

    # Three tasks carry telemetry (the NULL one is excluded).
    assert overall["tasks_measured"] == 3
    # Only the clean create run is well-formed (format_error_total == 0).
    assert overall["well_formed_tasks"] == 1
    assert overall["format_compliance_rate"] == round(1 / 3, 3)
    # Self-repair: 2 of 3 attempts succeeded across the window.
    assert overall["format_error_total"] == 3
    assert overall["repair_attempts"] == 3
    assert overall["repair_successes"] == 2
    assert overall["self_repair_success_rate"] == round(2 / 3, 3)

    # By-type breakdown.
    by_type = result["by_type"]
    assert set(by_type) == {"create", "fix"}

    create = by_type["create"]
    assert create["tasks_measured"] == 2
    assert create["well_formed_tasks"] == 1
    assert create["format_compliance_rate"] == 0.5
    # One repair attempt, one success — but below _SELF_REPAIR_MIN_ATTEMPTS so
    # self_repair_success_rate is suppressed (None) to prevent phantom drift signals.
    assert create["self_repair_success_rate"] is None

    fix = by_type["fix"]
    assert fix["tasks_measured"] == 1
    assert fix["well_formed_tasks"] == 0
    assert fix["format_compliance_rate"] == 0.0
    # Two repair attempts, below threshold — suppressed.
    assert fix["self_repair_success_rate"] is None


def test_format_compliance_metrics_null_summaries_excluded():
    """Tasks whose format_compliance_summary is NULL are not counted (neutral)."""
    tasks = [
        _fc_task("create", None),
        make_task(status=TaskStatus.COMPLETED, payloads=[{"type": "create"}]),
    ]
    result = compute_format_compliance_metrics(tasks)
    assert result["overall"]["tasks_measured"] == 0
    assert result["overall"]["format_compliance_rate"] is None
    assert result["overall"]["self_repair_success_rate"] is None
    assert result["by_type"] == {}


def test_format_compliance_metrics_zero_repair_denominator_is_none():
    """A measured task with format errors but no repair attempt yields a defined
    compliance rate (0.0) but a None self-repair rate (empty denominator)."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_miss_by_reason": {"no_matching_candidate": 1},
                "format_error_total": 1,
                "format_repair_attempts": 0,
                "format_repair_successes": 0,
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["tasks_measured"] == 1
    assert overall["format_compliance_rate"] == 0.0
    assert overall["repair_attempts"] == 0
    assert overall["self_repair_success_rate"] is None


def test_format_compliance_metrics_empty_list():
    """An empty terminal-task list returns an empty/None-rate structure."""
    result = compute_format_compliance_metrics([])
    assert result["overall"]["tasks_measured"] == 0
    assert result["overall"]["format_compliance_rate"] is None
    assert result["overall"]["self_repair_success_rate"] is None
    assert result["by_type"] == {}


def test_format_miss_by_reason_aggregation():
    """format_miss_by_reason is rolled up across tasks into the returned block."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_miss_by_reason": {"malformed_json": 2, "no_json_start": 1},
                "format_error_total": 3,
                "format_repair_attempts": 3,
                "format_repair_successes": 2,
            },
        ),
        _fc_task(
            "fix",
            {
                "format_miss_by_reason": {"malformed_json": 1, "schema_mismatch": 1},
                "format_error_total": 2,
                "format_repair_attempts": 2,
                "format_repair_successes": 1,
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["format_miss_by_reason"] == {
        "malformed_json": 3,
        "no_json_start": 1,
        "schema_mismatch": 1,
    }
    # repair miss dict is empty when no field present
    assert overall["format_repair_miss_by_reason"] == {}


def test_format_repair_miss_by_reason_aggregation():
    """format_repair_miss_by_reason (reopen failure reason) is rolled up separately."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 2,
                "format_repair_attempts": 2,
                "format_repair_successes": 0,
                "format_repair_miss_by_reason": {
                    "malformed_json": 1,
                    "no_json_start": 1,
                },
            },
        ),
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 0,
                "format_repair_miss_by_reason": {"malformed_json": 1},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["format_repair_miss_by_reason"] == {
        "malformed_json": 2,
        "no_json_start": 1,
    }
    # self_repair_success_rate unaffected — additive only
    assert overall["self_repair_success_rate"] == 0.0


def test_format_miss_by_reason_missing_fields_are_empty():
    """Tasks lacking format_miss_by_reason or format_repair_miss_by_reason contribute
    nothing to those rollups — backward-compatible with older summaries."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
                # neither reason field present
            },
        ),
        _fc_task("create", None),  # NULL summary — excluded entirely
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["format_miss_by_reason"] == {}
    assert overall["format_repair_miss_by_reason"] == {}
    assert overall["self_repair_success_rate"] == 1.0


def test_format_miss_by_reason_independent_from_repair_miss():
    """First-attempt and reopen reason dicts are tracked independently."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_miss_by_reason": {"schema_mismatch": 3},
                "format_error_total": 3,
                "format_repair_attempts": 3,
                "format_repair_successes": 1,
                "format_repair_miss_by_reason": {"malformed_json": 2},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["format_miss_by_reason"] == {"schema_mismatch": 3}
    assert overall["format_repair_miss_by_reason"] == {"malformed_json": 2}
    # success rate unchanged — additive only
    assert overall["self_repair_success_rate"] == round(1 / 3, 3)


def test_self_repair_rate_excludes_reopen_infra_failures():
    """Crashed reopens (infra failures) are excluded from the denominator.

    attempts=10, successes=6, infra_failures=4 → clean_attempts=6 → rate=1.0
    """
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 2,
                "format_repair_attempts": 10,
                "format_repair_successes": 6,
                "format_repair_infra_failures": 4,
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["repair_attempts"] == 10
    assert overall["repair_successes"] == 6
    assert overall["repair_infra_failures"] == 4
    # clean denominator = 10 - 4 = 6; rate = 6/6 = 1.0
    assert overall["self_repair_success_rate"] == 1.0


def test_self_repair_rate_infra_failures_equals_attempts_yields_none():
    """When infra_failures == attempts the clean denominator is 0 → rate is None."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 3,
                "format_repair_attempts": 5,
                "format_repair_successes": 0,
                "format_repair_infra_failures": 5,
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["repair_infra_failures"] == 5
    assert overall["self_repair_success_rate"] is None


def test_self_repair_rate_backward_compat_no_infra_failures():
    """Tasks without format_repair_infra_failures key behave identically to before.

    The field defaults to 0, so the denominator is unchanged and the legacy rate
    is preserved exactly.
    """
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 3,
                "format_repair_successes": 2,
                # no format_repair_infra_failures key — older agent
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["repair_infra_failures"] == 0
    assert overall["self_repair_success_rate"] == round(2 / 3, 3)


def test_self_repair_rate_infra_failures_by_schema_aggregated():
    """Per-schema infra failure counts are aggregated and surfaced in by_schema."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 5,
                "format_repair_successes": 3,
                "format_repair_infra_failures": 2,
                "format_repair_attempts_by_schema": {"ResearchPlan": 5},
                "format_repair_successes_by_schema": {"ResearchPlan": 3},
                "format_repair_infra_failures_by_schema": {"ResearchPlan": 2},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["repair_infra_failures"] == 2
    # clean_attempts = 5 - 2 = 3; rate = 3/3 = 1.0
    assert overall["self_repair_success_rate"] == 1.0
    by_schema = overall["by_schema"]
    assert "ResearchPlan" in by_schema
    assert by_schema["ResearchPlan"]["repair_infra_failures"] == 2
    # clean_attempts = 5 - 2 = 3 (at minimum-sample floor); rate = 3/3 = 1.0
    assert by_schema["ResearchPlan"]["self_repair_success_rate"] == 1.0


def test_format_compliance_by_schema_success_rate():
    """Per-schema self_repair_success_rate is derived from per-schema counts.

    Thinker schema: 2 success / (5 attempts - 1 infra) = 0.5
    Other schema:   3 success / (3 attempts - 0 infra) = 1.0
    Schema with only infra failures: None (clean denominator == 0)
    Each schema uses >=3 clean attempts to clear the minimum-sample floor.
    """
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 2,
                "format_repair_attempts": 8,
                "format_repair_successes": 5,
                "format_repair_infra_failures": 1,
                "format_repair_attempts_by_schema": {"thinker": 5, "Other": 3},
                "format_repair_successes_by_schema": {"thinker": 2, "Other": 3},
                "format_repair_infra_failures_by_schema": {"thinker": 1, "Other": 0},
            },
        ),
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 2,
                "format_repair_successes": 0,
                "format_repair_infra_failures": 2,
                "format_repair_attempts_by_schema": {"InfraOnly": 2},
                "format_repair_successes_by_schema": {"InfraOnly": 0},
                "format_repair_infra_failures_by_schema": {"InfraOnly": 2},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_schema = overall["by_schema"]

    # thinker: 2 success / (5 - 1) clean = 0.5
    assert by_schema["thinker"]["self_repair_success_rate"] == 0.5

    # Other: 3 success / (3 - 0) clean = 1.0
    assert by_schema["Other"]["self_repair_success_rate"] == 1.0

    # InfraOnly: clean_attempts = 2 - 2 = 0 → None
    assert by_schema["InfraOnly"]["self_repair_success_rate"] is None


def test_by_schema_self_repair_suppressed_below_min_attempts():
    """Per-schema self_repair_success_rate is None when clean attempts < 3.

    Low-volume schema labels (1–2 clean repairs per window) produce phantom
    0%/100% rates that look like regressions but are noise artifacts. The
    minimum-sample floor suppresses the rate to None so the operator dashboard
    does not display misleading signals.
    """
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 2,
                "format_repair_attempts": 4,
                "format_repair_successes": 2,
                "format_repair_infra_failures": 0,
                # strategic: 1 clean attempt — below floor → None
                # thinker:   2 clean attempts — below floor → None
                "format_repair_attempts_by_schema": {"strategic": 1, "thinker": 2},
                "format_repair_successes_by_schema": {"strategic": 1, "thinker": 2},
                "format_repair_infra_failures_by_schema": {"strategic": 0, "thinker": 0},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_schema = overall["by_schema"]

    # Both schemas are below the 3-attempt floor — rate must be None, not 0% or 100%
    assert by_schema["strategic"]["self_repair_success_rate"] is None
    assert by_schema["thinker"]["self_repair_success_rate"] is None


def test_per_schema_repair_miss_aggregated():
    """format_repair_miss_by_schema is aggregated across tasks and surfaced in by_schema."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 2,
                "format_repair_attempts": 3,
                "format_repair_successes": 1,
                "format_repair_miss_by_schema": {"thinker": 2},
                "format_repair_attempts_by_schema": {"thinker": 3},
                "format_repair_successes_by_schema": {"thinker": 1},
            },
        ),
        _fc_task(
            "fix",
            {
                "format_error_total": 1,
                "format_repair_attempts": 2,
                "format_repair_successes": 0,
                "format_repair_miss_by_schema": {"thinker": 1, "memo": 1},
                "format_repair_attempts_by_schema": {"thinker": 2},
                "format_repair_successes_by_schema": {},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_schema = overall["by_schema"]

    # thinker: 2 + 1 = 3 repair misses across both tasks
    assert "thinker" in by_schema
    assert by_schema["thinker"]["repair_miss"] == 3

    # memo: 1 repair miss from second task only
    assert "memo" in by_schema
    assert by_schema["memo"]["repair_miss"] == 1


def test_format_repair_miss_by_schema_missing_key_contributes_zero():
    """Tasks lacking format_repair_miss_by_schema contribute to reason totals only."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 0,
                "format_repair_miss_by_reason": {"malformed_json": 1},
                "format_repair_attempts_by_schema": {"thinker": 1},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_schema = overall["by_schema"]

    # thinker appears because of repair_attempts_by_schema, but repair_miss should be 0
    assert "thinker" in by_schema
    assert by_schema["thinker"]["repair_miss"] == 0
    assert overall["format_repair_miss_by_reason"] == {"malformed_json": 1}


def test_format_repair_miss_by_schema_in_schema_entry():
    """by_schema entries for schemas with repair-miss data expose the repair_miss field."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 3,
                "format_repair_attempts": 3,
                "format_repair_successes": 1,
                "format_repair_attempts_by_schema": {"thinker": 3},
                "format_repair_successes_by_schema": {"thinker": 1},
                "format_repair_miss_by_schema": {"thinker": 2},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    thinker = overall["by_schema"]["thinker"]
    assert thinker["repair_miss"] == 2
    assert thinker["repair_attempts"] == 3
    assert thinker["repair_successes"] == 1


def test_format_miss_by_schema_and_reason_aggregation():
    """format_miss_by_schema_and_reason merges nested reason counts across tasks.

    Task 1: strategic_review → {missing_required_keys: 3, extra_keys: 1}
    Task 2: strategic_review → {missing_required_keys: 2}, thinker → {bad_json: 1}
    Expected merged: strategic_review → {missing_required_keys: 5, extra_keys: 1},
                     thinker → {bad_json: 1}
    """
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 4,
                "format_repair_attempts": 2,
                "format_repair_successes": 1,
                "format_miss_by_schema_and_reason": {
                    "strategic_review": {
                        "missing_required_keys": 3,
                        "extra_keys": 1,
                    }
                },
            },
        ),
        _fc_task(
            "fix",
            {
                "format_error_total": 3,
                "format_repair_attempts": 2,
                "format_repair_successes": 0,
                "format_miss_by_schema_and_reason": {
                    "strategic_review": {"missing_required_keys": 2},
                    "thinker": {"bad_json": 1},
                },
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_reason = overall["format_miss_by_schema_and_reason"]

    # strategic_review: counts merged across both tasks
    assert by_reason["strategic_review"]["missing_required_keys"] == 5
    assert by_reason["strategic_review"]["extra_keys"] == 1

    # thinker: from second task only
    assert by_reason["thinker"]["bad_json"] == 1


def test_format_miss_by_schema_and_reason_absent_tasks_excluded():
    """Tasks lacking format_miss_by_schema_and_reason contribute nothing to that map."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 0,
                "format_miss_by_reason": {"missing_required_keys": 1},
                # no format_miss_by_schema_and_reason key
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    # map should be empty, not raise
    assert overall["format_miss_by_schema_and_reason"] == {}


def _fc_clean(task_type="create"):
    """Task with a clean (well-formed) format_compliance_summary."""
    return _fc_task(
        task_type,
        {
            "format_error_total": 0,
            "format_repair_attempts": 0,
            "format_repair_successes": 0,
        },
    )


def _fc_miss(task_type="create"):
    """Task with one format miss, one repair attempt, one success."""
    return _fc_task(
        task_type,
        {
            "format_error_total": 1,
            "format_repair_attempts": 1,
            "format_repair_successes": 1,
        },
    )


def test_format_compliance_reports_prior_window_and_trend():
    """Acceptance test: prior-window rate + trend appear in overall and by_type.

    Current window: 8 clean / 10 measured → compliance_rate = 0.8
    Prior window:   4 clean / 10 measured → compliance_rate = 0.4
    Delta = 0.4 > 0.05 → trend = "improving"
    """
    curr_tasks = [_fc_clean() for _ in range(8)] + [_fc_miss() for _ in range(2)]
    prev_tasks = [_fc_clean() for _ in range(4)] + [_fc_miss() for _ in range(6)]

    result = compute_format_compliance_metrics(
        curr_tasks, prev_terminal_tasks=prev_tasks
    )
    overall = result["overall"]

    assert overall["format_compliance_rate"] == pytest.approx(0.8)
    assert overall["prev_format_compliance_rate"] == pytest.approx(0.4)
    assert overall["trend"] == "improving"
    # Self-repair: curr has 2 attempts/successes → 1.0; prev has 6 attempts/successes → 1.0.
    assert overall["self_repair_success_rate"] == 1.0
    assert overall["prev_self_repair_success_rate"] == 1.0

    # by_type "create" block also carries the comparison fields.
    create = result["by_type"]["create"]
    assert "prev_format_compliance_rate" in create
    assert "prev_self_repair_success_rate" in create
    assert "trend" in create


def test_compute_format_compliance_metrics_trend_stable():
    """Delta ≤ 0.05 in either direction produces trend = 'stable'."""
    # curr = 0.7, prev = 0.72 → delta = -0.02 → stable
    curr = [_fc_clean() for _ in range(7)] + [_fc_miss() for _ in range(3)]
    prev = [_fc_clean() for _ in range(72)] + [_fc_miss() for _ in range(28)]

    result = compute_format_compliance_metrics(curr, prev_terminal_tasks=prev)
    assert result["overall"]["trend"] == "stable"


def test_compute_format_compliance_metrics_trend_declining():
    """Delta < -0.05 produces trend = 'declining'."""
    # curr = 0.4, prev = 0.8 → delta = -0.4 → declining
    curr = [_fc_clean() for _ in range(4)] + [_fc_miss() for _ in range(6)]
    prev = [_fc_clean() for _ in range(8)] + [_fc_miss() for _ in range(2)]

    result = compute_format_compliance_metrics(curr, prev_terminal_tasks=prev)
    assert result["overall"]["trend"] == "declining"
    assert result["overall"]["prev_format_compliance_rate"] == pytest.approx(0.8)


def test_compute_format_compliance_metrics_no_prev():
    """Without prev_terminal_tasks the snapshot keys are absent (backward-compat)."""
    tasks = [_fc_clean()]
    result = compute_format_compliance_metrics(tasks)
    overall = result["overall"]
    assert "trend" not in overall
    assert "prev_format_compliance_rate" not in overall
    assert "prev_self_repair_success_rate" not in overall


def test_compute_format_compliance_metrics_prev_empty_denominator():
    """Prev window with no measured tasks yields None prev rates and None trend."""
    curr = [_fc_clean()]
    prev = [_fc_task("create", None)]  # NULL summary → not measured

    result = compute_format_compliance_metrics(curr, prev_terminal_tasks=prev)
    overall = result["overall"]
    assert overall["prev_format_compliance_rate"] is None
    assert overall["trend"] is None


def test_compute_format_compliance_metrics_by_type_trend():
    """Per-type blocks carry prev/trend; types absent in curr but present in prev appear."""
    curr_tasks = [
        _fc_clean("create"),
        _fc_clean("create"),
        # No "fix" tasks in current window
    ]
    prev_tasks = [
        _fc_miss("create"),
        _fc_miss("create"),
        _fc_clean("fix"),  # "fix" only in prev
    ]

    result = compute_format_compliance_metrics(
        curr_tasks, prev_terminal_tasks=prev_tasks
    )

    # "create": curr=1.0, prev=0.0 → improving
    create = result["by_type"]["create"]
    assert create["format_compliance_rate"] == 1.0
    assert create["prev_format_compliance_rate"] == 0.0
    assert create["trend"] == "improving"

    # "fix": absent in curr → 0 tasks measured, rate None; prev=1.0 → trend None
    fix = result["by_type"]["fix"]
    assert fix["tasks_measured"] == 0
    assert fix["format_compliance_rate"] is None
    assert fix["prev_format_compliance_rate"] == 1.0
    assert fix["trend"] is None


def test_format_compliance_metrics_by_schema_aggregation():
    """Tasks with per-schema maps produce correctly merged by_schema in the block."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 2,
                "format_repair_successes": 1,
                "format_miss_by_schema": {"ResearchPlan": 1},
                "format_repair_attempts_by_schema": {"ResearchPlan": 2},
                "format_repair_successes_by_schema": {"ResearchPlan": 1},
            },
        ),
        _fc_task(
            "create",
            {
                "format_error_total": 0,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
                "format_miss_by_schema": {"StructuredVerdict": 0},
                "format_repair_attempts_by_schema": {
                    "ResearchPlan": 1,
                    "StructuredVerdict": 1,
                },
                "format_repair_successes_by_schema": {
                    "ResearchPlan": 1,
                    "StructuredVerdict": 1,
                },
            },
        ),
    ]
    result = compute_format_compliance_metrics(tasks)
    overall = result["overall"]

    assert overall["tasks_measured"] == 2
    by_schema = overall["by_schema"]

    assert by_schema["ResearchPlan"]["format_miss"] == 1
    assert by_schema["ResearchPlan"]["repair_attempts"] == 3
    assert by_schema["ResearchPlan"]["repair_successes"] == 2

    assert by_schema["StructuredVerdict"]["format_miss"] == 0
    assert by_schema["StructuredVerdict"]["repair_attempts"] == 1
    assert by_schema["StructuredVerdict"]["repair_successes"] == 1


def test_format_compliance_metrics_mixed_old_new_agents():
    """Tasks with and without by-schema maps both contribute to scalar totals
    but only new-agent tasks contribute to by_schema."""
    old_agent_task = _fc_task(
        "create",
        {
            "format_error_total": 1,
            "format_repair_attempts": 1,
            "format_repair_successes": 0,
            # No per-schema maps (old agent)
        },
    )
    new_agent_task = _fc_task(
        "create",
        {
            "format_error_total": 0,
            "format_repair_attempts": 1,
            "format_repair_successes": 1,
            "format_miss_by_schema": {},
            "format_repair_attempts_by_schema": {"ResearchPlan": 1},
            "format_repair_successes_by_schema": {"ResearchPlan": 1},
        },
    )
    result = compute_format_compliance_metrics([old_agent_task, new_agent_task])
    overall = result["overall"]

    # Both tasks contribute to scalar totals.
    assert overall["tasks_measured"] == 2
    assert overall["repair_attempts"] == 2
    assert overall["repair_successes"] == 1

    # Only new-agent task contributes to by_schema.
    by_schema = overall["by_schema"]
    assert "ResearchPlan" in by_schema
    assert by_schema["ResearchPlan"]["repair_attempts"] == 1

    # Empty by_schema when no tasks carry per-schema data.
    result_old_only = compute_format_compliance_metrics([old_agent_task])
    assert result_old_only["overall"]["by_schema"] == {}


def test_per_type_self_repair_drift_suppressed_below_min_attempts():
    """Per-type self_repair_success_rate is suppressed (None) when the type's clean
    repair attempts are below _SELF_REPAIR_MIN_ATTEMPTS, preventing phantom regression
    deltas from low-volume task types (e.g. strategic_review with ~2 repairs/window).

    The overall rate uses the full window and is NOT suppressed when it has enough
    clean attempts across all types combined.
    """
    # Build _SELF_REPAIR_MIN_ATTEMPTS - 1 clean repair tasks for "strategic_review"
    # (below threshold) and _SELF_REPAIR_MIN_ATTEMPTS tasks for "create" (at/above).
    low_volume_count = _SELF_REPAIR_MIN_ATTEMPTS - 1  # 9 — below the guard
    high_volume_count = _SELF_REPAIR_MIN_ATTEMPTS  # 10 — at the guard

    strategic_review_tasks = [
        _fc_task(
            "strategic_review",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
            },
        )
        for _ in range(low_volume_count)
    ]
    create_tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
            },
        )
        for _ in range(high_volume_count)
    ]

    result = compute_format_compliance_metrics(strategic_review_tasks + create_tasks)
    by_type = result["by_type"]

    # strategic_review has 9 clean attempts — below threshold — rate must be suppressed.
    assert by_type["strategic_review"]["repair_attempts"] == low_volume_count
    assert by_type["strategic_review"]["self_repair_success_rate"] is None

    # create has 10 clean attempts — at/above threshold — rate is NOT suppressed.
    assert by_type["create"]["repair_attempts"] == high_volume_count
    assert by_type["create"]["self_repair_success_rate"] == 1.0

    # The overall block spans both types (19 clean attempts) — not suppressed.
    overall = result["overall"]
    assert overall["repair_attempts"] == low_volume_count + high_volume_count
    assert overall["self_repair_success_rate"] == 1.0


def test_repair_token_accumulation_and_llm_usd():
    """repair_llm_usd is non-zero when repair_attempts > 0 and matches the pricing formula.

    Pricing: Sonnet 4.6 — 3e-6 USD/input-token, 15e-6 USD/output-token.
    Task: 1000 input tokens + 500 output tokens consumed by repair calls.
    Expected: 1000 * 3e-6 + 500 * 15e-6 = 0.003 + 0.0075 = 0.0105 USD.
    """
    task = _fc_task(
        "create",
        {
            "format_error_total": 1,
            "format_repair_attempts": 1,
            "format_repair_successes": 1,
            "format_repair_input_tokens": 1000,
            "format_repair_output_tokens": 500,
        },
    )
    overall = compute_format_compliance_metrics([task])["overall"]

    assert overall["repair_attempts"] == 1
    assert overall["repair_input_tokens"] == 1000
    assert overall["repair_output_tokens"] == 500
    assert overall["repair_llm_usd"] is not None
    assert overall["repair_llm_usd"] > 0
    expected_usd = round(1000 * 3e-6 + 500 * 15e-6, 6)
    assert overall["repair_llm_usd"] == pytest.approx(expected_usd)


def test_repair_llm_usd_none_when_no_repair_attempts():
    """repair_llm_usd is None when repair_attempts == 0 (no repair calls occurred)."""
    task = _fc_task(
        "create",
        {
            "format_error_total": 0,
            "format_repair_attempts": 0,
            "format_repair_successes": 0,
        },
    )
    overall = compute_format_compliance_metrics([task])["overall"]
    assert overall["repair_attempts"] == 0
    assert overall["repair_llm_usd"] is None
    assert overall["tokens_per_repair_attempt"] is None


def test_repair_token_backward_compat_missing_keys():
    """Tasks without token fields contribute 0 tokens — no error, backwards-compatible."""
    task = _fc_task(
        "create",
        {
            "format_error_total": 1,
            "format_repair_attempts": 2,
            "format_repair_successes": 1,
            # no format_repair_input_tokens / format_repair_output_tokens (older agent)
        },
    )
    overall = compute_format_compliance_metrics([task])["overall"]
    assert overall["repair_input_tokens"] == 0
    assert overall["repair_output_tokens"] == 0
    # repair_llm_usd is 0.0 (not None) because repair_attempts > 0 but tokens are 0
    assert overall["repair_llm_usd"] == pytest.approx(0.0)
    # tokens_per_repair_attempt is 0.0 (0 tokens / 2 attempts)
    assert overall["tokens_per_repair_attempt"] == pytest.approx(0.0)


def test_repair_tokens_accumulate_across_tasks():
    """Token counts are summed across all measured tasks."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
                "format_repair_input_tokens": 800,
                "format_repair_output_tokens": 300,
            },
        ),
        _fc_task(
            "fix",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 0,
                "format_repair_input_tokens": 600,
                "format_repair_output_tokens": 200,
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    assert overall["repair_input_tokens"] == 1400
    assert overall["repair_output_tokens"] == 500
    assert overall["tokens_per_repair_attempt"] == pytest.approx(
        (1400 + 500) / 2, rel=1e-3
    )
    expected_usd = round(1400 * 3e-6 + 500 * 15e-6, 6)
    assert overall["repair_llm_usd"] == pytest.approx(expected_usd)


def test_repair_tokens_by_schema_aggregation():
    """Per-schema token maps are summed and appear in by_schema."""
    tasks = [
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
                "format_repair_input_tokens": 500,
                "format_repair_output_tokens": 200,
                "format_repair_input_tokens_by_schema": {"memo": 500},
                "format_repair_output_tokens_by_schema": {"memo": 200},
            },
        ),
        _fc_task(
            "create",
            {
                "format_error_total": 1,
                "format_repair_attempts": 1,
                "format_repair_successes": 1,
                "format_repair_input_tokens": 300,
                "format_repair_output_tokens": 100,
                "format_repair_input_tokens_by_schema": {"memo": 200, "design": 100},
                "format_repair_output_tokens_by_schema": {"memo": 70, "design": 30},
            },
        ),
    ]
    overall = compute_format_compliance_metrics(tasks)["overall"]
    by_schema = overall["by_schema"]

    assert by_schema["memo"]["repair_input_tokens"] == 700
    assert by_schema["memo"]["repair_output_tokens"] == 270
    assert by_schema["design"]["repair_input_tokens"] == 100
    assert by_schema["design"]["repair_output_tokens"] == 30


# ── Park/wake operational health (feat: surface park/wake health) ────────────


def test_park_wake_failure_classes_are_recognized():
    """park_wake_timeout / park_wake_stranded are canonical recognized classes.

    They must carry human labels, appear in the display tuple, be budget-exempt
    INFRA classes, route to the infra taxonomy bucket, and be returned by the
    classifier for a task stamped with them — so failure distributions stop
    presenting them as generic code-like residual fingerprints.
    """
    from app.failure_classification import (
        ALL_FAILURE_CLASSES,
        INFRA_CLASSES,
        PARK_WAKE_STRANDED,
        PARK_WAKE_TIMEOUT,
        TAXONOMY_INFRA_RELATED,
        classify_failure,
        classify_failure_taxonomy,
    )

    # Canonical string values match the literals the expire/strand paths write.
    assert PARK_WAKE_TIMEOUT == "park_wake_timeout"
    assert PARK_WAKE_STRANDED == "park_wake_stranded"

    for cls in (PARK_WAKE_TIMEOUT, PARK_WAKE_STRANDED):
        assert cls in ALL_FAILURE_CLASSES, f"{cls} missing from display tuple"
        assert cls in INFRA_CLASSES, f"{cls} must be budget-exempt INFRA"
        # Human label present (and not the bare class-name fallback).
        assert cls in _FAILURE_CLASS_LABELS
        assert _FAILURE_CLASS_LABELS[cls] != cls

    # The classifier returns the recognized class for a stamped task, and the
    # taxonomy routes it to the infra bucket (not code_defect / residual).
    timed_out = make_task(
        status=TaskStatus.FAILED,
        latest_status_message="Park wake on compute_job_terminal timed out at deadline",
        context={
            "failure_category": PARK_WAKE_TIMEOUT,
            "failure_class": PARK_WAKE_TIMEOUT,
        },
    )
    stranded = make_task(
        status=TaskStatus.FAILED,
        latest_status_message="Parked task stranded with no live wake registration; failing",
        context={
            "failure_category": PARK_WAKE_STRANDED,
            "failure_class": PARK_WAKE_STRANDED,
        },
    )
    assert classify_failure(timed_out) == PARK_WAKE_TIMEOUT
    assert classify_failure(stranded) == PARK_WAKE_STRANDED
    assert classify_failure_taxonomy(timed_out) == TAXONOMY_INFRA_RELATED
    assert classify_failure_taxonomy(stranded) == TAXONOMY_INFRA_RELATED


@pytest.mark.asyncio
async def test_park_wake_health_metrics_surface(db_session):
    """Acceptance: the wake sweep's delivered/expired/stranded counts surface and
    the read-only park/wake health metrics expose the PARKED population + FIRED
    delivery backlog + stall signal; park_wake_* classify as recognized classes.

    Exercises the production seam end-to-end:
      * ``reaper_sweeps.sweep_deliver_task_wakes`` returns a ``WakeSweepResult``
        carrying delivered / expired / stranded / delivery_failed breakdowns;
      * ``digest_metrics.compute_park_wake_health`` surfaces parked count, oldest
        ages, FIRED-but-undelivered backlog, and the delivery-stalled signal.
    """
    from app.digest_metrics import (
        _PARK_WAKE_DELIVERY_STALL_SECONDS,
        compute_park_wake_health,
    )
    from app.failure_classification import PARK_WAKE_STRANDED, PARK_WAKE_TIMEOUT
    from app.models import (
        TaskWakeRegistration,
        WakeAction,
        WakeDeliveryState,
    )
    from app.reaper_sweeps import (
        WakeSweepResult,
        reset_wake_delivery_stall_state,
        sweep_deliver_task_wakes,
    )
    from app.task_lifecycle import _PARK_STRAND_SECONDS

    reset_wake_delivery_stall_state()

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Old enough that the read-only "delivery stalled" signal trips on the oldest
    # FIRED registration, but still deliverable by the sweep.
    fired_at = now - timedelta(seconds=_PARK_WAKE_DELIVERY_STALL_SECONDS + 60)

    # (1) Deliverable: PARKED + FIRED RESUME registration → delivered.
    deliver_task = make_task(
        project_id=project.id,
        status=TaskStatus.PARKED,
        title="park-deliver",
        parked_at=now - timedelta(minutes=5),
    )
    # (2) Expirable: PARKED + PENDING registration past its deadline → expired.
    expire_task = make_task(
        project_id=project.id,
        status=TaskStatus.PARKED,
        title="park-expire",
        parked_at=now - timedelta(minutes=5),
    )
    # (3) Strandable: PARKED past the strand threshold with NO live registration.
    strand_task = make_task(
        project_id=project.id,
        status=TaskStatus.PARKED,
        title="park-strand",
        parked_at=now - timedelta(seconds=_PARK_STRAND_SECONDS + 3600),
    )
    db_session.add_all([deliver_task, expire_task, strand_task])
    await db_session.commit()
    for t in (deliver_task, expire_task, strand_task):
        await db_session.refresh(t)

    fired_reg = TaskWakeRegistration(
        task_id=deliver_task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_action=WakeAction.RESUME,
        delivery_state=WakeDeliveryState.FIRED,
        fired_at=fired_at,
    )
    pending_reg = TaskWakeRegistration(
        task_id=expire_task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_action=WakeAction.RESUME,
        delivery_state=WakeDeliveryState.PENDING,
        deadline_at=now - timedelta(minutes=1),
    )
    db_session.add_all([fired_reg, pending_reg])
    await db_session.commit()

    # --- Read-only health BEFORE the sweep: PARKED population + FIRED backlog. --
    pre = await compute_park_wake_health(db_session, project_id=project.id)
    assert pre["parked_task_count"] == 3
    assert pre["oldest_parked_age_seconds"] >= _PARK_STRAND_SECONDS
    assert pre["fired_undelivered_count"] == 1
    assert pre["oldest_fired_age_seconds"] >= _PARK_WAKE_DELIVERY_STALL_SECONDS
    # delivery_stalled is now cycle-based (same counter as the reaper log/alert);
    # the stall counter was just reset at the start of this test so it's False here.
    assert pre["delivery_stalled"] is False
    assert pre["delivery_stall_threshold_seconds"] == _PARK_WAKE_DELIVERY_STALL_SECONDS

    # --- Run the leader-owned wake-delivery sweep. ---------------------------
    result = await sweep_deliver_task_wakes(db_session, now)

    # Backward-compatible int contract preserved (== wakes delivered)...
    assert isinstance(result, WakeSweepResult)
    assert result == 1
    # ...plus the per-pass breakdown now surfaces.
    assert result.delivered == 1
    assert result.expired == 1
    assert result.stranded == 1
    assert result.delivery_failed == 0
    log_fields = result.as_log_fields()
    assert log_fields["delivered"] == 1
    assert log_fields["expired"] == 1
    assert log_fields["stranded"] == 1

    # Snapshot before expire_all(): it expires ``project`` too (never
    # refreshed below), so the post-sweep ``project.id`` read would raise
    # MissingGreenlet under real ORM expiry.
    project_id = project.id
    db_session.expire_all()
    await db_session.refresh(deliver_task)
    await db_session.refresh(expire_task)
    await db_session.refresh(strand_task)

    # The deliverable task resumed; the expirable + strandable tasks failed with
    # the recognized, human-labelled park/wake classes (not residual code errors).
    assert deliver_task.status == TaskStatus.QUEUED
    assert expire_task.status == TaskStatus.FAILED
    assert (expire_task.context or {}).get("failure_class") == PARK_WAKE_TIMEOUT
    assert strand_task.status == TaskStatus.FAILED
    assert (strand_task.context or {}).get("failure_class") == PARK_WAKE_STRANDED

    # --- Read-only health AFTER the sweep: backlog drained, nothing parked. ---
    post = await compute_park_wake_health(db_session, project_id=project_id)
    assert post["parked_task_count"] == 0
    assert post["oldest_parked_age_seconds"] is None
    assert post["fired_undelivered_count"] == 0
    assert post["oldest_fired_age_seconds"] is None
    assert post["delivery_stalled"] is False
    # Additive new keys are always present (may be empty when no timeouts occurred).
    assert "timeout_by_cause" in post
    assert "timeout_cause_window_days" in post
    assert post["timeout_cause_window_days"] == 7


@pytest.mark.asyncio
async def test_park_wake_health_timeout_by_cause_breakdown(db_session):
    """timeout_by_cause buckets recently-expired park_wake_timeout tasks by cause.

    Inserts two failed tasks with different failure_detail values (and one with
    provisioning-starved vs ran-out-of-time diagnostics) and asserts the cause
    breakdown returned by compute_park_wake_health is correct.
    """
    from app.digest_metrics import compute_park_wake_health
    from app.models import Task as TaskModel

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Wake-lost failure (delivery bug).
    wake_lost_task = TaskModel(
        project_id=project.id,
        title="wake-lost timeout task",
        status=TaskStatus.FAILED,
        context={
            "failure_class": "park_wake_timeout",
            "failure_detail": "compute_job_terminal_wake_lost_after_job_completed",
            "park_wake_timeout_diagnostics": {
                "job_started_at": None,
                "job_created_at": "2026-01-01T00:00:00",
                "provisioning_seconds": 300.0,
                "execution_seconds": None,
                "job_terminal_by_deadline": True,
            },
        },
    )
    # Not-terminal / provisioning-starved (job_started_at is None).
    provisioning_starved_task = TaskModel(
        project_id=project.id,
        title="provisioning-starved timeout task",
        status=TaskStatus.FAILED,
        context={
            "failure_class": "park_wake_timeout",
            "failure_detail": "compute_job_terminal_job_not_terminal_by_deadline",
            "park_wake_timeout_diagnostics": {
                "job_started_at": None,
                "job_created_at": "2026-01-01T00:00:00",
                "provisioning_seconds": 300.0,
                "execution_seconds": None,
                "job_terminal_by_deadline": False,
            },
        },
    )
    # Not-terminal / ran-out-of-time (job_started_at is set).
    ran_out_task = TaskModel(
        project_id=project.id,
        title="ran-out-of-time timeout task",
        status=TaskStatus.FAILED,
        context={
            "failure_class": "park_wake_timeout",
            "failure_detail": "compute_job_terminal_job_not_terminal_by_deadline",
            "park_wake_timeout_diagnostics": {
                "job_started_at": "2026-01-01T00:05:00",
                "job_created_at": "2026-01-01T00:00:00",
                "provisioning_seconds": 300.0,
                "execution_seconds": 900.0,
                "job_terminal_by_deadline": False,
            },
        },
    )
    # A non-park_wake_timeout failed task — must NOT appear in timeout_by_cause.
    other_failed = TaskModel(
        project_id=project.id,
        title="other failed task",
        status=TaskStatus.FAILED,
        context={"failure_class": "CODE_ERROR"},
    )
    db_session.add_all(
        [wake_lost_task, provisioning_starved_task, ran_out_task, other_failed]
    )
    await db_session.commit()

    result = await compute_park_wake_health(db_session, project_id=project.id)

    by_cause = result["timeout_by_cause"]
    assert by_cause.get("compute_job_terminal_wake_lost_after_job_completed", 0) == 1, (
        f"expected 1 wake_lost; got {by_cause}"
    )
    assert by_cause.get("provisioning_starved", 0) == 1, (
        f"expected 1 provisioning_starved; got {by_cause}"
    )
    assert by_cause.get("ran_out_of_time", 0) == 1, (
        f"expected 1 ran_out_of_time; got {by_cause}"
    )
    # The non-park_wake_timeout task must not contribute to any cause bucket.
    total = sum(by_cause.values())
    assert total == 3, f"expected 3 total timeout tasks; got {total} in {by_cause}"


# ── compute_trial_arm_accumulation: arm_composition basis tracking ────────────


@pytest.mark.asyncio
async def test_trial_accumulation_reports_basis_composition(db_session):
    """arm_composition breaks down each arm into assignment-keyed vs receipt-fallback.

    When assignment_key != context_key, the result carries arm_composition with four
    counters: with_assigned, with_receipt, without_assigned, without_receipt. These
    let the verdict layer distinguish confounded legacy evidence from clean
    intent-to-treat evidence, ensuring a decided verdict cannot silently rest on
    fallback-receipt tasks that pre-date the deterministic holdback rollout.
    """
    from app.models import TaskStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = _utcnow() - timedelta(days=1)

    def _task(context):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=created,
            context=context,
        )

    tasks = [
        # Assignment-keyed WITH arm (2 tasks)
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": False,
                "_first_attempt_succeeded": False,
            }
        ),
        # Assignment-keyed WITHOUT arm (1 task)
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        # Receipt-fallback WITH arm — legacy, no assignment key (1 task)
        _task({"_had_repo_map": True, "_first_attempt_succeeded": True}),
        # Receipt-fallback WITHOUT arm — legacy, no assignment key (1 task)
        _task({"_had_repo_map": False, "_first_attempt_succeeded": False}),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        assignment_key="_repo_map_arm",
    )

    # Totals are unchanged — COALESCE-based arm partitioning covers all tasks.
    assert accum["with_n"] == 3  # 2 assigned + 1 legacy receipt
    assert accum["without_n"] == 2  # 1 assigned + 1 legacy receipt

    # Per-basis breakdown must be present and accurate.
    comp = accum["arm_composition"]
    assert comp["with_assigned"] == 2
    assert comp["with_receipt"] == 1
    assert comp["without_assigned"] == 1
    assert comp["without_receipt"] == 1


@pytest.mark.asyncio
async def test_trial_accumulation_no_composition_without_assignment_key(db_session):
    """When no assignment_key is provided, arm_composition is absent (receipt-only trial)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=_utcnow() - timedelta(days=1),
            context={"_had_repo_map": True, "_first_attempt_succeeded": True},
        )
    )
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        # no assignment_key
    )
    assert "arm_composition" not in accum


@pytest.mark.asyncio
async def test_trial_accumulation_counts_without_contaminated(db_session):
    """Without-arm tasks that carry a positive receipt despite holdback assignment are counted.

    A task assigned to the WITHOUT arm (_repo_map_arm=False) but with a truthy
    receipt (_had_repo_map=True) represents a contaminated holdback task — the
    suppression path was bypassed and the capability was delivered.  The
    accumulator must surface this as without_contaminated in arm_composition.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = _utcnow() - timedelta(days=1)

    def _task(context):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=created,
            context=context,
        )

    tasks = [
        # WITH arm — 2 assigned tasks
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": False,
                "_first_attempt_succeeded": False,
            }
        ),
        # WITHOUT arm, clean — holdback was respected (receipt=False)
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": False,
                "_first_attempt_succeeded": True,
            }
        ),
        # WITHOUT arm, contaminated — holdback was NOT respected (receipt=True)
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": True,
                "_first_attempt_succeeded": False,
            }
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        assignment_key="_repo_map_arm",
    )

    comp = accum["arm_composition"]
    # 2 contaminated holdback tasks (receipt=True in without arm)
    assert comp["without_contaminated"] == 2
    # 3 without-arm tasks total (1 clean + 2 contaminated)
    assert comp["without_assigned"] == 3
    # 0 clean without-receipt fallbacks in this scenario
    assert comp["without_receipt"] == 0


@pytest.mark.asyncio
async def test_trial_accumulation_no_contamination_when_receipt_false(db_session):
    """Without-arm tasks with receipt=False are NOT counted as contaminated."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = _utcnow() - timedelta(days=1)

    def _task(context):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=created,
            context=context,
        )

    tasks = [
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        # WITHOUT arm, receipt explicitly False — clean holdback
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": False,
                "_first_attempt_succeeded": True,
            }
        ),
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": False,
                "_first_attempt_succeeded": False,
            }
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        assignment_key="_repo_map_arm",
    )

    comp = accum["arm_composition"]
    assert comp["without_contaminated"] == 0
    assert comp["without_assigned"] == 2


@pytest.mark.asyncio
async def test_trial_accumulation_without_contaminated_absent_when_no_assignment_key(
    db_session,
):
    """When no assignment_key is provided, without_contaminated is absent (receipt-only path)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=_utcnow() - timedelta(days=1),
            context={"_had_repo_map": True, "_first_attempt_succeeded": True},
        )
    )
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        # no assignment_key — receipt-only trial path
    )
    # arm_composition is absent entirely when no assignment_key
    assert "arm_composition" not in accum


@pytest.mark.asyncio
async def test_trial_accumulation_reports_with_assigned_delivered(db_session):
    """with_assigned_delivered counts only WITH-arm tasks whose receipt key is TRUTHY.

    A task is 'delivered' when it is assigned to the WITH arm (assignment_key set) AND
    the receipt context_key is truthy (treatment actually ran). Tasks where the
    assignment key is set but the receipt key is absent entirely, OR present but
    explicitly false (e.g. a kill-switch-off task that still writes a falsy receipt),
    are assigned-but-undelivered and must NOT be counted in with_assigned_delivered.
    This counter feeds the engagement guard in capability_trial_verdict to suppress
    futility KILL verdicts when the treatment was never actually delivered.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    created = _utcnow() - timedelta(days=1)

    def _task(context):
        return make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            created_at=created,
            context=context,
        )

    tasks = [
        # WITH arm, delivered: assignment key set AND receipt key present
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": True,
                "_first_attempt_succeeded": True,
            }
        ),
        # WITH arm, assigned-but-undelivered: assignment key set but receipt key absent entirely
        _task({"_repo_map_arm": True, "_first_attempt_succeeded": False}),
        # WITH arm, assigned-but-undelivered: receipt key PRESENT but explicitly false
        # (e.g. a kill-switch-off task) — presence alone must not count as delivered.
        _task(
            {
                "_repo_map_arm": True,
                "_had_repo_map": False,
                "_first_attempt_succeeded": False,
            }
        ),
        # WITHOUT arm — not relevant to with_assigned_delivered
        _task(
            {
                "_repo_map_arm": False,
                "_had_repo_map": False,
                "_first_attempt_succeeded": True,
            }
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    accum = await compute_trial_arm_accumulation(
        db_session,
        project_id=project.id,
        context_key="_had_repo_map",
        assignment_key="_repo_map_arm",
    )

    comp = accum["arm_composition"]
    # All three WITH-arm tasks count as with_assigned
    assert comp["with_assigned"] == 3
    # Only the task with a truthy receipt counts as delivered — neither the missing-key
    # task nor the present-but-false-receipt task count.
    assert comp["with_assigned_delivered"] == 1
    # with_assigned_delivered must be strictly less than with_assigned when undelivered tasks exist
    assert comp["with_assigned_delivered"] < comp["with_assigned"]


# ---------------------------------------------------------------------------
# Guard: every digest payload key must have a named admin-ui consumer
# ---------------------------------------------------------------------------


def test_digest_payload_keys_have_named_consumer():
    """Every key the /api/admin/digest payload emits must have a named admin-ui
    consumer in DIGEST_PAYLOAD_METRIC_MANIFEST (i.e. a ``consumer`` entry, not
    just a ``status: pending`` or ``status: retired`` annotation).

    This is the enforcement layer that prevents consumerless compute functions
    from re-accumulating in the payload silently: adding a new key to
    ``_compute_digest`` without declaring a ``consumer`` in the manifest causes
    this test to fail immediately, blocking the PR before the metric ships.

    The test is DB-free and standalone-smoke compatible — it AST-parses
    admin_digest.py to extract the emitted keys and compares them against
    the manifest.
    """
    import ast

    from app.routers import admin_digest
    from app.routers.admin_digest import DIGEST_PAYLOAD_METRIC_MANIFEST

    source = open(admin_digest.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_compute_digest"
        ),
        None,
    )
    assert fn is not None, "could not find _compute_digest in admin_digest.py"

    emitted: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                emitted.add(k.value)

    assert emitted, (
        "AST extraction found no top-level string keys in the _compute_digest "
        "return dict — the extractor is broken"
    )

    consumerless = [
        key
        for key in sorted(emitted)
        if "consumer" not in DIGEST_PAYLOAD_METRIC_MANIFEST.get(key, {})
    ]
    assert not consumerless, (
        "These /api/admin/digest keys are emitted but lack a named 'consumer' "
        "in DIGEST_PAYLOAD_METRIC_MANIFEST. Each emitted key must declare which "
        "admin-ui component reads it (not just a pending/retired annotation): "
        f"{consumerless}"
    )

def test_multi_attempt_task_counts_once_in_cost_breakdown():
    """A multi-attempt task contributes task_count=1, priced_task_count=1,
    and merge_rate=1.0 even when it has two banked attempt-rows, not two."""
    from app.llm_cost_attribution import compute_llm_cost_breakdown
    from app.models import TaskStatus, bank_task_current_attempt

    now = _utcnow()

    # Build a task with one banked attempt and one live attempt (both on the
    # same anthropic/claude-sonnet-4-6 model — a priced combo).
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        vm_provisioned_at=now,
        vm_spot=True,
    )
    # Bank the first attempt into attempt_history so the task has two rows.
    bank_task_current_attempt(task, stopped_at=now)

    # Set up a second live attempt.
    task.api_call_summary = {
        "llm_call_count": 2,
        "total_input_tokens": 250,
        "total_output_tokens": 150,
    }
    task.vm_provisioned_at = now

    breakdown = compute_llm_cost_breakdown([task])
    totals = breakdown["totals"]

    # One task → task_count=1 (not 2 attempt-rows).
    assert totals["task_count"] == 1, (
        f"task_count should be 1 (one task), got {totals['task_count']}"
    )
    # One priced task → priced_task_count=1 (not 2 attempt-rows).
    assert totals["priced_task_count"] == 1, (
        f"priced_task_count should be 1 (one task), got {totals['priced_task_count']}"
    )
    assert totals["unpriced_task_count"] == 0

    # One merged task → merge_rate=1.0 (not 0.5 as if task_count were 2).
    assert totals["merged_count"] == 1
    assert totals["merge_rate"] == 1.0, (
        f"merge_rate should be 1.0 (1 merged / 1 task), got {totals['merge_rate']}"
    )

    # Model bucket should also count the task once.
    by_model = {(r["provider"], r["model"]): r for r in breakdown["by_model"]}
    row = by_model[("anthropic", "claude-sonnet-4-6")]
    assert row["task_count"] == 1
    assert row["merge_rate"] == 1.0
