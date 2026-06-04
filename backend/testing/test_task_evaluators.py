"""Tests for the pluggable per-type evaluation gate framework.

Covers the registry-driven strategy resolver and the synchronous strategy
implementations (none / forward-quality / metric / code-ci-review sentinel).
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
    ForwardQualityStrategy,
    MetricStrategy,
    NoneStrategy,
    evaluate_terminal_output,
    record_failed_gate,
    resolve_evaluation_strategy,
)


def _task(task_type: str) -> Task:
    return Task(payloads=[{"type": task_type, "base_branch": "main"}])


# ── Registry declarations ───────────────────────────────────────────────────


def test_every_task_type_declares_an_evaluation_strategy():
    """Each TaskType has an explicit, in-vocabulary evaluation declaration."""
    valid = {"code-ci-review", "forward-quality", "metric", "none"}
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
        "memo": "forward-quality",
        "verify_tests": "none",
        "compute_experiment": "none",
        "web_research": "forward-quality",
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
