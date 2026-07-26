"""Tests for the pluggable per-type evaluation gate framework.

Covers the registry-driven strategy resolver and the synchronous strategy
implementations (none / forward-quality / metric / artifact-delivery /
code-ci-review sentinel).
The completion-path integration (memo/perf handlers acting on a failed gate)
is exercised in test_memo_completion.py and test_perf_completion.py.
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
    ArtifactDeliveryStrategy,
    ForwardQualityStrategy,
    MetricStrategy,
    NoneStrategy,
    _declared_success_criterion,
    _declared_success_criteria,
    evaluate_compute_success_criteria,
    evaluate_terminal_output,
    record_failed_gate,
    resolve_evaluation_strategy,
)


def _task(task_type: str) -> Task:
    return Task(payloads=[{"type": task_type, "base_branch": "main"}])


# ── Registry declarations ───────────────────────────────────────────────────


def test_every_task_type_declares_an_evaluation_strategy():
    """Each TaskType has an explicit, in-vocabulary evaluation declaration."""
    valid = {
        "code-ci-review",
        "forward-quality",
        "metric",
        "artifact-delivery",
        "structured-verdict",
        "none",
    }
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
        "release_audit_challenge": "none",
        "release_merge_grade": "none",
        "memo": "forward-quality",
        "verify_tests": "none",
        "compute_experiment": "artifact-delivery",
        "web_research": "forward-quality",
        "design": "forward-quality",
        "repo_bootstrap": "none",
        "salvage_bundle": "code-ci-review",
        "capability_proof_trial": "none",
        "criterion_coverage_trial": "none",
        "walkthrough_e2e": "none",
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


def test_metric_perf_measurement_non_dict_element_fails_cleanly():
    """A perf measurement array containing a non-dict element (string, number, etc.)
    is malformed and fails the gate with a clear reason, not an AttributeError."""
    task = Task(payloads=[{"type": "perf", "base_branch": "main", "max_severity": "medium"}])
    # Mixed array with valid dict and non-dict string element
    v = MetricStrategy().evaluate(
        task, json.dumps([{"severity": "low", "title": "x"}, "high"])
    )
    assert v.passed is False
    assert "malformed" in v.reason.lower()
    # Non-dict number element
    v = MetricStrategy().evaluate(
        task, json.dumps([3, {"severity": "low"}])
    )
    assert v.passed is False
    assert "malformed" in v.reason.lower()


# ── ArtifactDeliveryStrategy: compute_experiment shape-awareness ────────────


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


def test_artifact_delivery_compute_experiment_valid_result_passes():
    """A terminal compute result passes the artifact-delivery gate."""
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"), _compute_result()
    )
    assert v.passed is True
    assert "SUCCESS" in v.reason


def test_metric_compute_experiment_budget_kill_is_honest_terminal_pass():
    """A spend-cap kill carries an outcome + cost, so it is an honest terminal
    outcome that PASSES the gate (the experiment did its measured job)."""
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(outcome="SPEND_CAP_EXCEEDED", status="FAILED"),
    )
    assert v.passed is True


def test_metric_compute_experiment_deadline_kill_is_honest_terminal_pass():
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(outcome="TIMED_OUT", status="TIMED_OUT"),
    )
    assert v.passed is True


def test_metric_compute_experiment_accrued_cost_fallback_passes():
    """When final_cost_usd is absent, accrued_cost_usd is the fallback measure."""
    payload = json.dumps(
        {"job_id": "j", "outcome": "SUCCESS", "accrued_cost_usd": 0.1}
    )
    v = ArtifactDeliveryStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is True


def test_metric_compute_experiment_zero_cost_passes():
    """A measured cost of 0.0 is a real measurement (not 'no measurement')."""
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"), _compute_result(final_cost_usd=0.0)
    )
    assert v.passed is True


def test_metric_compute_experiment_missing_outcome_fails():
    """No terminal outcome → no terminal job state → gate fails closed."""
    payload = json.dumps({"job_id": "j", "final_cost_usd": 0.4})
    v = ArtifactDeliveryStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is False
    assert "terminal" in v.reason.lower()


def test_metric_compute_experiment_null_outcome_fails():
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"), _compute_result(outcome=None)
    )
    assert v.passed is False


def test_metric_compute_experiment_missing_cost_fails():
    """No measured final/accrued cost → gate fails closed."""
    payload = json.dumps({"job_id": "j", "outcome": "SUCCESS"})
    v = ArtifactDeliveryStrategy().evaluate(_task("compute_experiment"), payload)
    assert v.passed is False


def test_metric_compute_experiment_non_numeric_cost_fails():
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"),
        _compute_result(final_cost_usd="n/a", accrued_cost_usd=None),
    )
    assert v.passed is False


def test_metric_compute_experiment_empty_and_unparseable_fail():
    for bad in ("", "   ", "not json at all", "[1, 2, 3]"):
        v = ArtifactDeliveryStrategy().evaluate(_task("compute_experiment"), bad)
        assert v.passed is False, bad


def test_metric_compute_experiment_does_not_use_perf_array_path():
    """A compute_experiment must NOT be accepted by the perf severity-array path —
    a severity array carries no terminal outcome and must fail the compute gate."""
    findings = json.dumps([{"severity": "high", "title": "x"}])
    v = ArtifactDeliveryStrategy().evaluate(_task("compute_experiment"), findings)
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


def test_declared_success_criterion_metric_kind_returns_trio():
    """A kind='metric' criterion is read back as its (metric, op, threshold) trio."""
    task = _criterion_task("accuracy", ">=", 0.9)
    assert _declared_success_criterion(task) == ("accuracy", ">=", 0.9)


def test_declared_success_criterion_artifact_produced_translates_to_file_count():
    """kind='artifact_produced' translates to a durable_file_count >= N check so the
    existing numeric pipeline evaluates non-scalar (model/dataset) experiments."""
    task = Task(
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "success_criterion": {"kind": "artifact_produced"},
            }
        ]
    )
    assert _declared_success_criterion(task) == ("durable_file_count", ">=", 1.0)


def test_declared_success_criterion_artifact_produced_custom_min_count():
    task = Task(
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "success_criterion": {
                    "kind": "artifact_produced",
                    "min_file_count": 3,
                },
            }
        ]
    )
    assert _declared_success_criterion(task) == ("durable_file_count", ">=", 3.0)


def test_multiple_compute_criteria_are_and_evaluated_with_typed_observations():
    task = Task(
        payloads=[{
            "type": "compute_experiment",
            "success_criteria": [
                {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
                {"metric": "latency_ms", "operator": "<=", "threshold": 100},
            ],
        }]
    )
    assert _declared_success_criteria(task) == [
        ("accuracy", ">=", 0.9),
        ("latency_ms", "<=", 100.0),
    ]
    verdict = evaluate_compute_success_criteria(
        task, json.dumps({"accuracy": 0.95, "latency_ms": 120})
    )
    assert verdict is not None
    assert verdict["operator"] == "AND"
    assert verdict["met"] is False
    assert [item["observation_status"] for item in verdict["criteria"]] == [
        "MEASURED",
        "MEASURED",
    ]


def test_compute_criterion_reports_missing_and_invalid_metric_reasons():
    task = _criterion_task("accuracy", ">=", 0.9)
    absent = evaluate_compute_success_criteria(task, json.dumps({"loss": 1.0}))
    assert absent["criteria"][0]["observation_status"] == "UNMEASURED"
    assert absent["criteria"][0]["reason_code"] == "METRIC_KEY_ABSENT"

    invalid = evaluate_compute_success_criteria(
        task, json.dumps({"accuracy": "not-a-number"})
    )
    assert invalid["criteria"][0]["observation_status"] == "INVALID"
    assert invalid["criteria"][0]["reason_code"] == "METRIC_NOT_NUMERIC"

    ambiguous = evaluate_compute_success_criteria(
        task, json.dumps({"accuracy": 0.95, " Accuracy ": 0.7})
    )
    assert ambiguous["criteria"][0]["observation_status"] == "INVALID"
    assert ambiguous["criteria"][0]["reason_code"] == "AMBIGUOUS_METRIC_KEY"


def test_metric_strategy_does_not_report_multi_criteria_met_when_later_one_fails():
    task = Task(
        payloads=[{
            "type": "compute_experiment",
            "success_criteria": [
                {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
                {"metric": "latency_ms", "operator": "<=", "threshold": 100},
            ],
        }]
    )
    artifact = json.dumps({
        "outcome": "SUCCESS",
        "final_cost_usd": 0.4,
        "accuracy": 0.95,
        "latency_ms": 120,
    })
    verdict = MetricStrategy().evaluate(task, artifact)
    assert verdict.passed is True
    assert "1/2 met" in verdict.reason
    assert "failed: latency_ms" in verdict.reason
    assert "all 2 declared success criteria met" not in verdict.reason


def test_artifact_delivery_reports_all_multi_criteria_when_they_pass():
    task = Task(
        payloads=[{
            "type": "compute_experiment",
            "success_criteria": [
                {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
                {"metric": "latency_ms", "operator": "<=", "threshold": 100},
            ],
        }]
    )
    artifact = json.dumps({
        "outcome": "SUCCESS",
        "final_cost_usd": 0.4,
        "accuracy": 0.95,
        "latency_ms": 80,
    })
    verdict = ArtifactDeliveryStrategy().evaluate(task, artifact)
    assert verdict.passed is True
    assert "all 2 declared success criteria met" in verdict.reason


def test_compute_criterion_met_passes_with_reason():
    """A declared criterion on a MEASURABLE (job-level) metric that the reported
    result satisfies PASSES, and the verdict reason names the satisfied goal.

    Uses ``final_cost_usd`` — a key the REAL terminal COMPUTE_RESULT actually
    carries — rather than injecting an experiment-domain metric the agent never
    emits."""
    artifact = _compute_result(final_cost_usd=0.42)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("final_cost_usd", "<=", 0.9), artifact
    )
    assert v.passed is True
    assert "criterion met" in v.reason
    assert "final_cost_usd <= 0.9" in v.reason


def test_compute_criterion_unmet_fails_with_operator_readable_reason():
    """A declared criterion on a measurable metric that the result misses still
    passes artifact delivery and records the unmet goal in the reason."""
    artifact = _compute_result(final_cost_usd=10.0)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("final_cost_usd", "<=", 5.0), artifact
    )
    assert v.passed is True
    assert "not met" in v.reason
    assert "final_cost_usd=10.0" in v.reason
    assert "<= 5.0" in v.reason


def test_compute_criterion_missing_measurable_metric_fails_closed():
    """An absent declared metric no longer gates delivered artifact completion."""
    # spend_cap_usd is a measurable key, but this result omits it.
    artifact = json.dumps({"job_id": "j", "outcome": "SUCCESS", "final_cost_usd": 0.4})
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("spend_cap_usd", ">=", 1.0), artifact
    )
    assert v.passed is True
    assert "not reported" in v.reason


def test_compute_criterion_reported_metric_met_passes_with_goal_named():
    """A reported experiment-domain metric (accuracy) that the agent flattened to
    a top-level key and that MEETS its declared goal PASSES, naming the goal."""
    artifact = _compute_result(accuracy=0.93)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("accuracy", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "criterion met" in v.reason
    assert "accuracy >= 0.9" in v.reason


def test_compute_criterion_reported_metric_unmet_fails():
    """An unmet reported experiment metric passes artifact delivery but names
    the unmet metric, observed value, operator, and threshold."""
    artifact = _compute_result(accuracy=0.83)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("accuracy", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "not met" in v.reason
    assert "accuracy=0.83" in v.reason
    assert ">= 0.9" in v.reason


def test_compute_criterion_absent_reported_metric_fails_closed():
    """An absent experiment-domain metric is recorded without blocking delivery."""
    artifact = _compute_result()  # no 'accuracy' key reported
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("accuracy", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "not reported" in v.reason
    assert "accuracy >= 0.9" in v.reason


def test_compute_criterion_non_numeric_reported_metric_fails_closed():
    """A declared metric present but non-numeric in the artifact fails closed."""
    artifact = _compute_result(accuracy="n/a")
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("accuracy", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "not reported" in v.reason


def test_compute_criterion_can_gate_on_cost_metric():
    """The criterion reads any measurable numeric result key by name, incl. cost."""
    artifact = _compute_result(final_cost_usd=10.0)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("final_cost_usd", "<=", 5.0), artifact
    )
    assert v.passed is True
    assert "final_cost_usd=10.0" in v.reason


def test_compute_criterion_case_insensitive_match_met():
    """A criterion declared as 'auc' is matched when the artifact emits 'AUC'."""
    artifact = _compute_result(AUC=0.95)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("auc", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "criterion met" in v.reason
    assert "auc >= 0.9" in v.reason


def test_compute_criterion_case_insensitive_match_unmet():
    """Case-insensitive match works even when the criterion is not met."""
    artifact = _compute_result(Accuracy=0.7)
    v = ArtifactDeliveryStrategy().evaluate(
        _criterion_task("accuracy", ">=", 0.9), artifact
    )
    assert v.passed is True
    assert "not met" in v.reason
    assert "accuracy=0.7" in v.reason


def test_compute_criterion_ambiguous_case_match_fails_closed():
    """When 'auc' and 'AUC' both exist and criterion is 'Auc' (no exact match),
    the ambiguity guard treats the metric as absent rather than guessing."""
    payload = json.dumps(
        {"job_id": "j", "outcome": "SUCCESS", "final_cost_usd": 0.1, "auc": 0.95, "AUC": 0.85}
    )
    # Declared as 'Auc' — no exact match; case-fold finds both 'auc' and 'AUC'.
    v = ArtifactDeliveryStrategy().evaluate(_criterion_task("Auc", ">=", 0.9), payload)
    assert v.passed is True
    # ambiguous → metric treated as absent
    assert "not reported" in v.reason


def test_compute_no_criterion_preserves_presence_based_pass():
    """With no declared criterion, the gate keeps its presence-based behavior."""
    v = ArtifactDeliveryStrategy().evaluate(
        _task("compute_experiment"), _compute_result()
    )
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
    v = ArtifactDeliveryStrategy().evaluate(task, _compute_result(accuracy=0.5))
    assert v.passed is True


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



# ── per-finding challenge: prompt builder (read-only, single finding) ─────────


def test_finding_challenge_prompt_is_single_finding_and_ends_with_one_json():
    """build_finding_challenge_prompt frames ONE finding read-only and ends with
    exactly one JSON object (reusing the grader's end-with-one-JSON convention)."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        build_finding_challenge_prompt,
    )

    finding = {"title": "Stale ref to foo()", "prompt": "fix foo"}
    prompt = build_finding_challenge_prompt(
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, finding, "absent"
    )
    assert "SINGLE research finding" in prompt
    assert "read-only" in prompt
    # The first verdict being challenged is named.
    assert "'absent'" in prompt
    # The finding body is embedded.
    assert "Stale ref to foo()" in prompt
    # Ends with exactly one JSON verdict object the parser can read back.
    assert prompt.rstrip().endswith("}")
    assert '"agreement"' in prompt
    assert prompt.count("FINDING START") == 1


def test_finding_challenge_prompt_boundary_specific_framing():
    """Each boundary selects its own framing language."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        build_finding_challenge_prompt,
    )

    absent = build_finding_challenge_prompt(
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, {"title": "x"}, "absent"
    )
    assert "MOVED or RENAMED" in absent
    decay = build_finding_challenge_prompt(
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY, {"title": "x"}, "LOW"
    )
    assert "LOW actionability" in decay


def test_finding_challenge_prompt_includes_question_when_supplied():
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        build_finding_challenge_prompt,
    )

    prompt = build_finding_challenge_prompt(
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        {"title": "x"},
        "absent",
        question="is the reaper sweep correct",
    )
    assert "is the reaper sweep correct" in prompt
    assert "REQUEST START" in prompt


# ── per-finding challenge: verdict parser (fail-closed) ──────────────────────


def test_parse_finding_challenge_disagrees():
    """A structured 'disagrees' second opinion is recorded with all six fields."""
    from app.task_evaluators import (
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        FindingChallengeRecord,
        parse_finding_challenge_verdict,
    )

    logs = (
        "I checked git log; foo() moved to bar.py.\n"
        '{"verdict": "present", "agreement": "disagrees", '
        '"reasoning": "code was renamed, not deleted"}'
    )
    rec = parse_finding_challenge_verdict(
        logs, "absent", CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "sidecar-1"
    )
    assert isinstance(rec, FindingChallengeRecord)
    assert rec.challenged_boundary == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
    assert rec.first_verdict == "absent"
    assert rec.second_opinion_verdict == "present"
    assert rec.agreement == "disagrees"
    assert "renamed" in rec.reason
    assert rec.sidecar_task_id == "sidecar-1"
    assert rec.schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert rec.source_task_id is None
    assert rec.produced_by == "challenge_sidecar"
    assert rec.to_context() == {
        "challenged_boundary": CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        "first_verdict": "absent",
        "second_opinion_verdict": "present",
        "agreement": "disagrees",
        "reason": rec.reason,
        "sidecar_task_id": "sidecar-1",
        "schema_version": FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        "source_task_id": None,
        "produced_by": "challenge_sidecar",
        "finding_hash": None,
    }


def test_parse_finding_challenge_propagates_source_task_id():
    from app.task_evaluators import (
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        parse_finding_challenge_verdict,
    )

    rec = parse_finding_challenge_verdict(
        '{"verdict": "present", "agreement": "disagrees", "reasoning": "renamed"}',
        "absent",
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        "sidecar-2",
        source_task_id="task-123",
    )
    ctx = rec.to_context()
    assert rec.schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert rec.source_task_id == "task-123"
    assert ctx["schema_version"] == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert ctx["source_task_id"] == "task-123"
    assert ctx["produced_by"] == "challenge_sidecar"


def test_parse_finding_challenge_agrees():
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        parse_finding_challenge_verdict,
    )

    logs = '{"verdict": "absent", "agreement": "agrees", "reasoning": "truly gone"}'
    rec = parse_finding_challenge_verdict(
        logs, "absent", CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "s2"
    )
    assert rec.agreement == "agrees"
    assert rec.second_opinion_verdict == "absent"


def test_parse_finding_challenge_not_completed_is_indeterminate():
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        parse_finding_challenge_verdict,
    )

    rec = parse_finding_challenge_verdict(
        "anything", "LOW", CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY, "s3",
        completed=False,
    )
    assert rec.agreement == "indeterminate"
    assert rec.sidecar_task_id == "s3"


def test_parse_finding_challenge_unparseable_and_empty_are_indeterminate():
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        parse_finding_challenge_verdict,
    )

    for logs in ("no machine verdict here", ""):
        rec = parse_finding_challenge_verdict(
            logs, "absent", CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "s4"
        )
        assert rec.agreement == "indeterminate", logs
        assert rec.first_verdict == "absent"


def test_parse_finding_challenge_unknown_agreement_collapses_to_indeterminate():
    """A parsed agreement outside the recognized set fails closed."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        parse_finding_challenge_verdict,
    )

    logs = '{"verdict": "present", "agreement": "maybe", "reasoning": "unsure"}'
    rec = parse_finding_challenge_verdict(
        logs, "absent", CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "s5"
    )
    assert rec.agreement == "indeterminate"


# ── FindingChallengeRecord: schema version and provenance ────────────────────


def test_finding_challenge_record_schema_version_default():
    """Newly constructed records carry schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        FindingChallengeRecord,
    )

    rec = FindingChallengeRecord(
        challenged_boundary=CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        first_verdict="absent",
        second_opinion_verdict="present",
        agreement="disagrees",
        reason="code was renamed",
        sidecar_task_id="sc-1",
    )
    assert rec.schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert rec.schema_version == 1
    assert rec.source_task_id is None
    assert rec.produced_by == "challenge_sidecar"


def test_finding_challenge_record_to_context_includes_version_and_provenance():
    """to_context() emits schema_version, source_task_id, and produced_by."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        FindingChallengeRecord,
    )

    rec = FindingChallengeRecord(
        challenged_boundary=CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        first_verdict="absent",
        second_opinion_verdict="present",
        agreement="disagrees",
        reason="code was renamed",
        sidecar_task_id="sc-1",
        source_task_id="src-task-abc",
    )
    ctx = rec.to_context()
    assert ctx["schema_version"] == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert ctx["source_task_id"] == "src-task-abc"
    assert ctx["produced_by"] == "challenge_sidecar"
    # Existing fields preserved.
    assert ctx["challenged_boundary"] == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
    assert ctx["first_verdict"] == "absent"
    assert ctx["agreement"] == "disagrees"
    assert ctx["sidecar_task_id"] == "sc-1"


def test_parse_finding_challenge_stamps_versioned_record():
    """parse_finding_challenge_verdict stamps schema_version and source_task_id
    on a successfully parsed (merged) record so downstream consumers can
    distinguish versioned first-class output from legacy fallback records."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        parse_finding_challenge_verdict,
    )

    logs = (
        'I checked git log; foo() moved to bar.py.\n'
        '{"verdict": "present", "agreement": "disagrees", '
        '"reasoning": "code was renamed, not deleted"}'
    )
    rec = parse_finding_challenge_verdict(
        logs,
        "absent",
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        "sidecar-99",
        completed=True,
        source_task_id="research-task-xyz",
    )
    assert rec.schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert rec.source_task_id == "research-task-xyz"
    assert rec.produced_by == "challenge_sidecar"
    assert rec.agreement == "disagrees"
    # Verify to_context() carries the versioned fields through.
    ctx = rec.to_context()
    assert ctx["schema_version"] == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert ctx["source_task_id"] == "research-task-xyz"
    assert ctx["produced_by"] == "challenge_sidecar"


def test_parse_finding_challenge_backward_compat_no_source_task_id():
    """Callers that omit source_task_id still produce a valid versioned record
    with source_task_id=None — existing call sites are unaffected."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        FINDING_CHALLENGE_RECORD_SCHEMA_VERSION,
        parse_finding_challenge_verdict,
    )

    logs = '{"verdict": "actionable", "agreement": "disagrees", "reasoning": "finding is specific"}'
    rec = parse_finding_challenge_verdict(
        logs, "LOW", CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY, "sc-legacy"
    )
    assert rec.schema_version == FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    assert rec.source_task_id is None
    assert rec.produced_by == "challenge_sidecar"


def test_parse_finding_challenge_prefers_last_verdict_object():
    """When sidecar output contains multiple JSON objects with a ``verdict``
    key, parse_finding_challenge_verdict picks the LAST one so an echoed
    schema example at the top does not shadow the agent's real answer."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        parse_finding_challenge_verdict,
    )

    logs = (
        # Echoed schema fragment at the top — should be ignored.
        'Here is the schema: {"verdict": "<classification>", "agreement": "agrees", '
        '"reasoning": "<justification>"}\n'
        "I investigated the codebase and the function moved to utils.py.\n"
        # Real verdict at the end.
        '{"verdict": "present", "agreement": "disagrees", "reasoning": "code was relocated"}'
    )
    rec = parse_finding_challenge_verdict(
        logs, "absent", CHALLENGE_BOUNDARY_REPRODUCE_ABSENT, "sc-last"
    )
    assert rec.agreement == "disagrees"
    assert rec.second_opinion_verdict == "present"
    assert "relocated" in rec.reason


def test_parse_finding_challenge_stamps_finding_hash():
    """finding_hash kwarg is stored on the returned record and exposed via to_context()."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        parse_finding_challenge_verdict,
    )

    logs = '{"verdict": "actionable", "agreement": "disagrees", "reasoning": "specific bug"}'
    rec = parse_finding_challenge_verdict(
        logs,
        "LOW",
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        "sc-hash",
        finding_hash="abc123def456789",
    )
    assert rec.finding_hash == "abc123def456789"
    assert rec.to_context()["finding_hash"] == "abc123def456789"


def test_finding_challenge_record_to_context_includes_finding_hash():
    """to_context() emits finding_hash; defaults to None when not set."""
    from app.task_evaluators import (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        FindingChallengeRecord,
    )

    rec_no_hash = FindingChallengeRecord(
        challenged_boundary=CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        first_verdict="absent",
        second_opinion_verdict="present",
        agreement="disagrees",
        reason="relocated",
        sidecar_task_id="sc-1",
    )
    assert rec_no_hash.to_context()["finding_hash"] is None

    rec_with_hash = FindingChallengeRecord(
        challenged_boundary=CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        first_verdict="absent",
        second_opinion_verdict="present",
        agreement="disagrees",
        reason="relocated",
        sidecar_task_id="sc-2",
        finding_hash="deadbeef12345678",
    )
    assert rec_with_hash.to_context()["finding_hash"] == "deadbeef12345678"


# ── build_adversarial_challenge_manifest ──────────────────────────────────────


class TestBuildAdversarialChallengeManifest:
    """build_adversarial_challenge_manifest: compact, bounded, valid JSON manifest."""

    def _finding(self, title="Risk finding", risk="high", scope="medium", **kwargs):
        return {
            "title": title,
            "description": f"Description of {title}",
            "risk": risk,
            "scope": scope,
            "hash": f"hash-{title[:20].replace(' ', '-')}",
            **kwargs,
        }

    def test_single_finding_produces_valid_json(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        items, json_str = build_adversarial_challenge_manifest([self._finding()])
        assert len(items) == 1
        parsed = json.loads(json_str)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_hash_preserved_in_manifest(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        f = self._finding(title="Specific finding")
        f["hash"] = "stable-hash-abc"
        items, json_str = build_adversarial_challenge_manifest([f])
        assert items[0]["hash"] == "stable-hash-abc"
        assert "stable-hash-abc" in json_str

    def test_compact_fields_only_no_complexity_signals(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        f = self._finding()
        f["_complexity_signals"] = {"loc": 5000, "files": 30}
        f["already_spawned"] = False
        f["actionability_grade"] = "HIGH"
        items, _ = build_adversarial_challenge_manifest([f])
        assert "_complexity_signals" not in items[0]
        assert "already_spawned" not in items[0]
        assert "actionability_grade" not in items[0]
        assert "hash" in items[0]
        assert "title" in items[0]

    def test_long_description_is_truncated(self):
        from app.task_evaluators import (
            _ADVERSARIAL_MANIFEST_DESC_CAP,
            build_adversarial_challenge_manifest,
        )

        f = self._finding()
        f["description"] = "x" * 2000
        items, _ = build_adversarial_challenge_manifest([f])
        assert len(items[0]["description"]) == _ADVERSARIAL_MANIFEST_DESC_CAP

    def test_many_findings_stay_within_budget(self):
        from app.task_evaluators import (
            _ADVERSARIAL_MANIFEST_CHAR_BUDGET,
            build_adversarial_challenge_manifest,
        )

        findings = [
            self._finding(title=f"Finding {i}", risk="high")
            for i in range(50)
        ]
        for f in findings:
            f["description"] = "A" * 400
        items, json_str = build_adversarial_challenge_manifest(findings)
        assert len(json_str) <= _ADVERSARIAL_MANIFEST_CHAR_BUDGET
        assert json.loads(json_str)  # valid JSON

    def test_provenance_fields_preserved(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        f = self._finding()
        f["verification_basis_downgraded_from"] = "high"
        f["needs_revalidation"] = True
        items, _ = build_adversarial_challenge_manifest([f])
        assert items[0].get("verification_basis_downgraded_from") == "high"
        assert items[0].get("needs_revalidation") is True

    def test_findings_without_hash_are_skipped(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        f = self._finding()
        f["hash"] = ""
        items, json_str = build_adversarial_challenge_manifest([f])
        assert items == []
        assert json_str == "[]"

    def test_result_json_array_is_always_parseable(self):
        from app.task_evaluators import build_adversarial_challenge_manifest

        _, json_str = build_adversarial_challenge_manifest([])
        assert json.loads(json_str) == []
