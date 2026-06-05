"""Tests for the pluggable per-type evaluation gate framework.

Covers the registry-driven strategy resolver and the synchronous strategy
implementations (none / forward-quality / metric / code-ci-review sentinel).
The completion-path integration (memo/perf handlers acting on a failed gate)
is exercised in test_memo_completion.py, test_perf_completion.py, and
test_panel_review_completion.py.
"""

import json

from app.models import Task, TaskStatus
from app.task_capabilities import (
    EVALUATION_STRATEGY_BY_TASK_TYPE,
    TASK_CAPABILITIES,
    TaskType,
)
from app.task_evaluators import (
    CODE_CI_REVIEW_KEY,
    CodeCiReviewStrategy,
    EvaluationVerdict,
    ForwardQualityStrategy,
    MetricStrategy,
    NoneStrategy,
    StructuredVerdictStrategy,
    evaluate_terminal_output,
    record_failed_gate,
    resolve_evaluation_strategy,
)


def _task(task_type: str) -> Task:
    return Task(payloads=[{"type": task_type, "base_branch": "main"}])


# ── Registry declarations ───────────────────────────────────────────────────


def test_every_task_type_declares_an_evaluation_strategy():
    """Each TaskType has an explicit, in-vocabulary evaluation declaration."""
    valid = {"code-ci-review", "forward-quality", "metric", "structured-verdict", "none"}
    assert set(EVALUATION_STRATEGY_BY_TASK_TYPE) == {t.value for t in TaskType}
    for cap in TASK_CAPABILITIES:
        assert cap.evaluation in valid, (cap.id, cap.evaluation)


def test_evaluation_declarations_match_expected_assignment():
    """Lock the per-type strategy assignment from the task spec: code/PR types
    keep code-ci-review (unchanged merge gate), knowledge/audit types get
    forward-quality, perf gets metric, plan/no-gate types get none."""
    assert EVALUATION_STRATEGY_BY_TASK_TYPE == {
        "create": "code-ci-review",
        "review": "none",
        "panel_review": "structured-verdict",
        "fix": "code-ci-review",
        "plan": "none",
        "research": "forward-quality",
        "strategic_review": "forward-quality",
        "design_doc_update": "forward-quality",
        "security_audit": "forward-quality",
        "qa": "code-ci-review",
        "deps": "code-ci-review",
        "docs": "code-ci-review",
        "migrate": "code-ci-review",
        "smoketest": "none",
        "refactor": "code-ci-review",
        "debug": "forward-quality",
        "perf": "metric",
        "investigate": "forward-quality",
        "self_audit": "forward-quality",
        "release_audit": "forward-quality",
        "memo": "forward-quality",
        "verify_tests": "none",
        "compute_experiment": "metric",
        "web_research": "forward-quality",
        "design": "forward-quality",
    }


# ── resolve_evaluation_strategy (registry-driven) ───────────────────────────


def test_resolver_returns_registry_declared_strategy_for_each_type():
    """The resolver returns the strategy whose key matches the registry-declared
    evaluation value — for EVERY known task type. This is the registry-driven
    contract: selection is never a hardwired type→strategy if/elif."""
    for task_type, declared_key in EVALUATION_STRATEGY_BY_TASK_TYPE.items():
        strategy = resolve_evaluation_strategy(task_type)
        assert strategy.key == declared_key, task_type


def test_resolver_falls_back_to_none_for_unknown_type():
    strategy = resolve_evaluation_strategy("future_unknown_type")
    assert strategy.key == "none"


def test_resolver_falls_back_to_none_for_missing_type():
    assert resolve_evaluation_strategy(None).key == "none"


# ── NoneStrategy ────────────────────────────────────────────────────────────


def test_none_strategy_always_passes():
    v = NoneStrategy().evaluate(_task("plan"), "")
    assert v.passed is True
    assert v.strategy == "none"


# ── CodeCiReviewStrategy (sentinel) ─────────────────────────────────────────


def test_code_ci_review_is_a_passthrough_sentinel():
    """Code tasks keep their CI + review merge gate — this strategy never fails
    or blocks; it reports a delegated pass so the framework can't double-gate."""
    v = CodeCiReviewStrategy().evaluate(_task("create"), "anything")
    assert v.passed is True
    assert v.strategy == CODE_CI_REVIEW_KEY
    assert v.delegated is True


def test_code_task_resolves_to_sentinel_and_passes():
    v = evaluate_terminal_output(_task("create"), "")
    assert v.delegated is True
    assert v.passed is True


# ── ForwardQualityStrategy ──────────────────────────────────────────────────


def test_forward_quality_empty_artifact_fails():
    for empty in ("", "   \n\t  ", None):
        v = ForwardQualityStrategy().evaluate(_task("research"), empty)
        assert v.passed is False
        assert "empty" in v.reason.lower()


def test_forward_quality_memo_unparseable_fails():
    v = ForwardQualityStrategy().evaluate(_task("memo"), "this is not json at all")
    assert v.passed is False
    assert "memo" in v.reason.lower()


def test_forward_quality_memo_valid_passes():
    memo = json.dumps(
        {"summary": "s", "reasoning": "r", "citations": [], "tags": []}
    )
    v = ForwardQualityStrategy().evaluate(_task("memo"), memo)
    assert v.passed is True


def test_forward_quality_memo_missing_keys_fails():
    """A JSON object that is not a memo (missing summary/reasoning) fails even
    though it is structurally JSON."""
    v = ForwardQualityStrategy().evaluate(_task("memo"), '{"hello": "world"}')
    assert v.passed is False


def test_forward_quality_generic_accepts_structured_findings():
    findings = json.dumps([{"severity": "high", "title": "x"}])
    v = ForwardQualityStrategy().evaluate(_task("security_audit"), findings)
    assert v.passed is True


def test_forward_quality_generic_accepts_substantive_prose():
    prose = "This audit reviewed the auth layer and found the token check sound."
    v = ForwardQualityStrategy().evaluate(_task("self_audit"), prose)
    assert v.passed is True


def test_forward_quality_generic_rejects_thin_error_token():
    v = ForwardQualityStrategy().evaluate(_task("research"), "error")
    assert v.passed is False


# ── MetricStrategy ──────────────────────────────────────────────────────────


def test_metric_no_measurement_fails():
    v = MetricStrategy().evaluate(_task("perf"), "no findings array at all")
    assert v.passed is False
    assert "measurement" in v.reason.lower()


def test_metric_empty_artifact_fails():
    v = MetricStrategy().evaluate(_task("perf"), "")
    assert v.passed is False


def test_metric_with_structured_measurement_passes():
    findings = json.dumps(
        [{"severity": "high", "title": "slow path", "area": "db"}]
    )
    v = MetricStrategy().evaluate(_task("perf"), findings)
    assert v.passed is True
    assert "1 item" in v.reason


def test_metric_perf_max_severity_criterion_passes_and_fails():
    task = Task(payloads=[{"type": "perf", "base_branch": "main", "max_severity": "medium"}])
    v_pass = MetricStrategy().evaluate(
        task, json.dumps([{"severity": "low", "title": "slow path"}])
    )
    v_fail = MetricStrategy().evaluate(
        task, json.dumps([{"severity": "high", "title": "slow path"}])
    )
    assert v_pass.passed is True
    assert "max severity medium met" in v_pass.reason
    assert v_fail.passed is False
    assert "high exceeds allowed medium" in v_fail.reason


# ── MetricStrategy: compute_experiment shape-awareness ──────────────────────


def _compute_result(**overrides) -> str:
    base = {
        "job_id": "job-123",
        "outcome": "SUCCESS",
        "final_cost_usd": 0.42,
        "accrued_cost_usd": 0.42,
        "status": "SUCCEEDED",
    }
    base.update(overrides)
    return json.dumps(base)


def test_metric_compute_experiment_valid_result_passes():
    """A terminal compute result (outcome + final cost) passes the metric gate."""
    v = MetricStrategy().evaluate(
        _task("compute_experiment"), _compute_result()
    )
    assert v.passed is True
    assert "SUCCESS" in v.reason


def test_metric_compute_experiment_budget_kill_is_honest_terminal_pass():
    """A spend-cap kill carries an outcome + cost, so it is an honest terminal
    outcome that PASSES the gate (the experiment did its measured job)."""
    v = MetricStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(outcome="SPEND_CAP_EXCEEDED", status="FAILED"),
    )
    assert v.passed is True


def test_metric_compute_experiment_deadline_kill_is_honest_terminal_pass():
    v = MetricStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(outcome="TIMED_OUT", status="TIMED_OUT"),
    )
    assert v.passed is True


def test_metric_compute_experiment_accrued_cost_fallback_passes():
    """When final_cost_usd is absent, accrued_cost_usd is the fallback measure."""
    payload = json.dumps(
        {"job_id": "j", "outcome": "SUCCESS", "accrued_cost_usd": 0.1}
    )
    v = MetricStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is True


def test_metric_compute_experiment_zero_cost_passes():
    """A measured cost of 0.0 is a real measurement (not 'no measurement')."""
    v = MetricStrategy().evaluate(
        _task("compute_experiment"), _compute_result(final_cost_usd=0.0)
    )
    assert v.passed is True


def test_metric_compute_experiment_missing_outcome_fails():
    """No terminal outcome → no terminal job state → gate fails closed."""
    payload = json.dumps({"job_id": "j", "final_cost_usd": 0.4})
    v = MetricStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is False
    assert "terminal" in v.reason.lower()


def test_metric_compute_experiment_null_outcome_fails():
    v = MetricStrategy().evaluate(
        _task("compute_experiment"), _compute_result(outcome=None)
    )
    assert v.passed is False


def test_metric_compute_experiment_missing_cost_fails():
    """No measured final/accrued cost → gate fails closed."""
    payload = json.dumps({"job_id": "j", "outcome": "SUCCESS"})
    v = MetricStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is False


def test_metric_compute_experiment_non_numeric_cost_fails():
    v = MetricStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(final_cost_usd="n/a", accrued_cost_usd=None),
    )
    assert v.passed is False


def test_metric_compute_experiment_empty_and_unparseable_fail():
    for bad in ("", "   ", "not json at all", "[1, 2, 3]"):
        v = MetricStrategy().evaluate(_task("compute_experiment"), bad)
        assert v.passed is False, bad


def test_metric_compute_experiment_does_not_use_perf_array_path():
    """A compute_experiment must NOT be accepted by the perf severity-array path —
    a severity array carries no terminal outcome and must fail the compute gate."""
    findings = json.dumps([{"severity": "high", "title": "x"}])
    v = MetricStrategy().evaluate(_task("compute_experiment"), findings)
    assert v.passed is False


# ── MetricStrategy: compute_experiment success criterion ────────────────────


def _criterion_task(metric: str, op: str, threshold: float) -> Task:
    return Task(
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "success_criterion": {
                    "metric": metric,
                    "operator": op,
                    "threshold": threshold,
                },
            }
        ]
    )


def test_compute_criterion_met_passes_with_reason():
    """A declared criterion that the reported metric satisfies PASSES and the
    verdict reason names the satisfied goal."""
    artifact = _compute_result(accuracy=0.95)
    v = MetricStrategy().evaluate(_criterion_task("accuracy", ">=", 0.9), artifact)
    assert v.passed is True
    assert "criterion met" in v.reason
    assert "accuracy >= 0.9" in v.reason


def test_compute_criterion_unmet_fails_with_operator_readable_reason():
    """A declared criterion the reported metric misses FAILS the gate honestly."""
    artifact = _compute_result(accuracy=0.5)
    v = MetricStrategy().evaluate(_criterion_task("accuracy", ">=", 0.9), artifact)
    assert v.passed is False
    assert "not met" in v.reason
    assert "accuracy=0.5" in v.reason
    assert ">= 0.9" in v.reason


def test_compute_criterion_missing_metric_fails_closed():
    """A declared criterion whose metric is absent from the result fails closed —
    an unmeasurable goal must not pass on presence alone."""
    artifact = _compute_result()  # no 'accuracy' key
    v = MetricStrategy().evaluate(_criterion_task("accuracy", ">=", 0.9), artifact)
    assert v.passed is False
    assert "not evaluable" in v.reason


def test_compute_criterion_can_gate_on_cost_metric():
    """The criterion reads any numeric result key by name, including cost."""
    artifact = _compute_result(final_cost_usd=10.0)
    v = MetricStrategy().evaluate(
        _criterion_task("final_cost_usd", "<=", 5.0), artifact
    )
    assert v.passed is False
    assert "final_cost_usd=10.0" in v.reason


def test_compute_no_criterion_preserves_presence_based_pass():
    """With no declared criterion, the gate keeps its presence-based behavior."""
    v = MetricStrategy().evaluate(_task("compute_experiment"), _compute_result())
    assert v.passed is True
    assert "criterion" not in v.reason


def test_compute_malformed_criterion_is_ignored():
    """A malformed criterion (unknown operator) is not interpretable, so the gate
    falls back to presence-based behavior rather than failing the task."""
    task = Task(
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "success_criterion": {
                    "metric": "accuracy",
                    "operator": "approx",
                    "threshold": 0.9,
                },
            }
        ]
    )
    v = MetricStrategy().evaluate(task, _compute_result(accuracy=0.5))
    assert v.passed is True


# ── StructuredVerdictStrategy ───────────────────────────────────────────────


def test_structured_verdict_missing_contract_fails():
    v = StructuredVerdictStrategy().evaluate(_task("panel_review"), '{"verdict":"APPROVE"}')
    assert v.passed is False
    assert "structured verdict" in v.reason.lower()


def test_structured_verdict_valid_panel_review_passes():
    payload = json.dumps(
        {
            "overall_verdict": "APPROVE",
            "summary": "All four review lenses passed.",
            "lenses": {
                "correctness": {"verdict": "APPROVE", "summary": "Logic checks clean."},
                "security": {"verdict": "APPROVE", "summary": "No auth or injection regressions."},
                "code_quality": {"verdict": "APPROVE", "summary": "Patterns remain consistent."},
                "test_coverage": {"verdict": "APPROVE", "summary": "Changed behavior is covered."},
            },
        }
    )
    v = StructuredVerdictStrategy().evaluate(_task("panel_review"), payload)
    assert v.passed is True
    assert v.strategy == "structured-verdict"


def _panel_lenses(*, drop_field: tuple[str, str] | None = None) -> dict:
    lenses = {
        "correctness": {"verdict": "APPROVE", "summary": "Logic checks clean."},
        "security": {"verdict": "APPROVE", "summary": "No auth or injection regressions."},
        "code_quality": {"verdict": "APPROVE", "summary": "Patterns remain consistent."},
        "test_coverage": {"verdict": "APPROVE", "summary": "Changed behavior is covered."},
    }
    if drop_field is not None:
        lens_name, field_name = drop_field
        lenses[lens_name].pop(field_name, None)
    return lenses


def test_structured_verdict_present_lens_missing_verdict_fails():
    payload = json.dumps(
        {
            "overall_verdict": "APPROVE",
            "summary": "All four review lenses present.",
            "lenses": _panel_lenses(drop_field=("correctness", "verdict")),
        }
    )
    v = StructuredVerdictStrategy().evaluate(_task("panel_review"), payload)
    assert v.passed is False
    assert "structured verdict" in v.reason.lower()


def test_structured_verdict_present_lens_missing_summary_fails():
    payload = json.dumps(
        {
            "overall_verdict": "APPROVE",
            "summary": "All four review lenses present.",
            "lenses": _panel_lenses(drop_field=("security", "summary")),
        }
    )
    v = StructuredVerdictStrategy().evaluate(_task("panel_review"), payload)
    assert v.passed is False
    assert "structured verdict" in v.reason.lower()


def test_structured_verdict_non_json_garbage_fails():
    v = StructuredVerdictStrategy().evaluate(
        _task("panel_review"), "This PR looks fine to me, approving!"
    )
    assert v.passed is False
    assert "structured verdict" in v.reason.lower()
    assert "empty" not in v.reason.lower()


# ── record_failed_gate ──────────────────────────────────────────────────────


def test_record_failed_gate_marks_failed_and_records_state():
    task = _task("memo")
    task.status = TaskStatus.COMPLETED
    verdict = EvaluationVerdict(False, "bad output", "forward-quality")

    record_failed_gate(task, verdict)

    assert task.status == TaskStatus.FAILED
    assert "forward-quality" in (task.latest_status_message or "")
    assert "bad output" in (task.latest_status_message or "")
    gate = task.context["evaluation_gate"]
    assert gate == {
        "strategy": "forward-quality",
        "passed": False,
        "reason": "bad output",
    }


def test_record_failed_gate_preserves_existing_context():
    task = _task("perf")
    task.context = {"existing": "value"}
    record_failed_gate(task, EvaluationVerdict(False, "no measurement", "metric"))
    assert task.context["existing"] == "value"
    assert task.context["evaluation_gate"]["strategy"] == "metric"


# ── grader rubric: optional relevance lens ──────────────────────────────────


def test_grader_prompt_omits_relevance_lens_without_question():
    """With no question the rubric carries only the 4 base lenses."""
    from app.task_evaluators import build_forward_quality_grader_prompt

    prompt = build_forward_quality_grader_prompt("web_research", "some artifact")
    assert "RELEVANCE" not in prompt
    assert "REQUEST START" not in prompt
    assert "4. COHERENCE" in prompt


def test_grader_prompt_adds_relevance_lens_with_question():
    """A supplied question grows a RELEVANCE lens carrying the asked topic so
    the grader can judge on-topic-ness."""
    from app.task_evaluators import build_forward_quality_grader_prompt

    prompt = build_forward_quality_grader_prompt(
        "web_research", "some artifact", question="state of quantum computing"
    )
    assert "5. RELEVANCE" in prompt
    assert "state of quantum computing" in prompt
    assert "REQUEST START" in prompt
