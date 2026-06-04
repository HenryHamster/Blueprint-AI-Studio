"""Standalone tests for the per-task-type terminal outcome primitive.

Covers each task-type family (code-merge, research, audit, memo, verify_tests)
for success / neutral / failure, plus unknown-type forward-compatibility and
the OUTCOME_EVALUATORS registry override seam. Pure in-memory Task objects, no
DB — runs under ``bash scripts/test-backend.sh --standalone``.
"""

import uuid

import pytest

from app.landed_signal import (
    LANDED_ON_INTEGRATION_KEY,
    LANDED_SIGNAL_KEY,
)
from app.models import Task, TaskStatus
from app.task_outcomes import (
    KIND_AUDIT_FINDINGS,
    KIND_CODE_MERGE,
    KIND_MEMO_ARTIFACT,
    KIND_READ_ONLY_UNCLASSIFIED,
    KIND_RESEARCH_QUALITY,
    KIND_UNKNOWN,
    KIND_VERIFICATION_VERDICT,
    OUTCOME_EVALUATORS,
    TaskOutcome,
    build_task_evaluation_summary,
    evaluate_task_outcome,
    is_successful_outcome,
)


def _make_task(
    task_type: str,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    context: dict | None = None,
    plan_output=None,
    latest_status_message: str | None = None,
) -> Task:
    """Build an in-memory Task with the given type and terminal signals."""
    return Task(
        project_id=uuid.uuid4(),
        title=f"{task_type} task",
        payloads=[{"type": task_type}],
        status=status,
        created_by="ORCHESTRATOR",
        context=context or {},
        plan_output=plan_output,
        latest_status_message=latest_status_message,
    )


def _merged_context() -> dict:
    """Context for a task that truly landed on the integration branch."""
    return {LANDED_SIGNAL_KEY: True, LANDED_ON_INTEGRATION_KEY: True}


# ─────────────────────────────────────────────────────────────────────────────
# Write / code-merge family
# ─────────────────────────────────────────────────────────────────────────────


def test_write_merged_is_success_code_merge():
    task = _make_task("create", status=TaskStatus.COMPLETED, context=_merged_context())
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_failed_is_failure_code_merge():
    task = _make_task("create", status=TaskStatus.FAILED)
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_genuine_cancelled_is_failure():
    # A CANCELLED task with no non-genuine marker counts as a genuine failure.
    task = _make_task("fix", status=TaskStatus.CANCELLED)
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_CODE_MERGE)


def test_write_non_genuine_cancelled_is_neutral():
    # Cascade-killed CANCELLED tasks are NOT failures (mirrors _counts_as_failure).
    task = _make_task(
        "create",
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)


def test_write_completed_no_merge_is_neutral():
    # COMPLETED but never landed a PR — a healthy no-op, not success or failure.
    task = _make_task("create", status=TaskStatus.COMPLETED, context={})
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_success_matches_is_merged_completion_exactly():
    from app.landed_signal import is_merged_completion

    merged = _make_task("refactor", context=_merged_context())
    unmerged = _make_task("refactor", context={})
    assert is_successful_outcome(merged) == is_merged_completion(merged) is True
    assert is_successful_outcome(unmerged) == is_merged_completion(unmerged) is False


# ─────────────────────────────────────────────────────────────────────────────
# Research family
# ─────────────────────────────────────────────────────────────────────────────


def test_research_actionable_findings_is_success():
    task = _make_task(
        "research",
        plan_output=[
            {"title": "fix: do the thing", "actionability_grade": "HIGH"},
            {"title": "fix: another", "actionability_grade": "MEDIUM"},
        ],
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_RESEARCH_QUALITY)
    assert is_successful_outcome(task) is True


def test_research_no_findings_is_neutral():
    task = _make_task("research", plan_output=[])
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_RESEARCH_QUALITY)


def test_research_untitled_findings_is_neutral():
    # Findings without a title are not usable output.
    task = _make_task("research", plan_output=[{"actionability_grade": "LOW"}])
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_RESEARCH_QUALITY)


def test_research_failed_is_failure():
    task = _make_task("research", status=TaskStatus.FAILED, plan_output=[])
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_RESEARCH_QUALITY)


# ─────────────────────────────────────────────────────────────────────────────
# Audit family (security_audit / perf / self_audit / release_audit)
# ─────────────────────────────────────────────────────────────────────────────


def test_security_audit_findings_dict_shape_is_success():
    task = _make_task(
        "security_audit",
        plan_output={"findings": [{"severity": "critical", "title": "SQLi"}]},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_AUDIT_FINDINGS)


def test_perf_findings_dict_shape_is_success():
    task = _make_task(
        "perf", plan_output={"findings": [{"severity": "high", "title": "N+1"}]}
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_AUDIT_FINDINGS)


def test_self_audit_findings_list_shape_is_success():
    # self_audit stores a bare list of normalized findings.
    task = _make_task(
        "self_audit",
        plan_output=[{"system_id": "task_lifecycle", "score": 70}],
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_AUDIT_FINDINGS)


def test_audit_empty_findings_is_neutral():
    task = _make_task("security_audit", plan_output={"findings": []})
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_AUDIT_FINDINGS)


def test_audit_failed_is_failure():
    task = _make_task(
        "release_audit", status=TaskStatus.FAILED, plan_output={"findings": []}
    )
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_AUDIT_FINDINGS)


# ─────────────────────────────────────────────────────────────────────────────
# Memo family
# ─────────────────────────────────────────────────────────────────────────────


def test_memo_with_artifact_is_success():
    task = _make_task(
        "memo",
        plan_output={"memo": {"summary": "reasoning", "citations": [], "tags": []}},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_MEMO_ARTIFACT)
    assert is_successful_outcome(task) is True


def test_memo_missing_artifact_is_neutral():
    # COMPLETED memo with no parseable reasoning — terminal but uncitable.
    task = _make_task("memo", plan_output=None)
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_MEMO_ARTIFACT)


def test_memo_failed_is_failure():
    task = _make_task("memo", status=TaskStatus.FAILED)
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_MEMO_ARTIFACT)


# ─────────────────────────────────────────────────────────────────────────────
# verify_tests family
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_tests_pass_verdict_is_success():
    task = _make_task("verify_tests", plan_output={"detected": True, "passed": True})
    assert evaluate_task_outcome(task) == TaskOutcome(
        "success", KIND_VERIFICATION_VERDICT
    )


def test_verify_tests_fail_verdict_is_failure():
    task = _make_task(
        "verify_tests",
        plan_output={"detected": True, "passed": False, "output": "1 test failed"},
    )
    assert evaluate_task_outcome(task) == TaskOutcome(
        "failure", KIND_VERIFICATION_VERDICT
    )


def test_verify_tests_undetected_verdict_is_neutral():
    task = _make_task("verify_tests", plan_output={"detected": False})
    assert evaluate_task_outcome(task) == TaskOutcome(
        "neutral", KIND_VERIFICATION_VERDICT
    )


def test_verify_tests_verdict_from_context_is_success():
    # Verdict mirrored onto context (forward-compat home) is honored too.
    task = _make_task(
        "verify_tests",
        context={"merge_test_verdict": {"detected": True, "passed": True}},
    )
    assert evaluate_task_outcome(task) == TaskOutcome(
        "success", KIND_VERIFICATION_VERDICT
    )


def test_verify_tests_no_verdict_is_neutral():
    task = _make_task("verify_tests", plan_output=None)
    assert evaluate_task_outcome(task) == TaskOutcome(
        "neutral", KIND_VERIFICATION_VERDICT
    )


# ─────────────────────────────────────────────────────────────────────────────
# Forward-compatibility & registry seam
# ─────────────────────────────────────────────────────────────────────────────


def test_unknown_task_type_is_neutral():
    task = _make_task("totally_new_type", plan_output={"findings": [{"x": 1}]})
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_UNKNOWN)


def test_unclassified_read_only_type_is_neutral():
    # A known read-only type with no built-in evaluator (e.g. plan) is neutral.
    task = _make_task("plan", status=TaskStatus.COMPLETED)
    assert evaluate_task_outcome(task) == TaskOutcome(
        "neutral", KIND_READ_ONLY_UNCLASSIFIED
    )


def test_outcome_evaluators_override_is_consulted_first():
    sentinel = TaskOutcome("success", "custom_kind")
    OUTCOME_EVALUATORS["research"] = lambda _task: sentinel
    try:
        # Override wins even over the built-in research evaluator's neutral.
        task = _make_task("research", plan_output=[])
        assert evaluate_task_outcome(task) is sentinel
    finally:
        OUTCOME_EVALUATORS.pop("research", None)


# ─────────────────────────────────────────────────────────────────────────────
# Operator-facing evaluation summaries (build_task_evaluation_summary)
# ─────────────────────────────────────────────────────────────────────────────


def test_summary_verify_tests_pass_verdict():
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "tests": [{"name": "pytest", "status": "pass", "message": "ok"}],
            "output": "PASS all tests",
        },
    )
    assert summary is not None
    assert summary["verdict"] == "pass"
    assert summary["evidence"][0]["label"] == "Detected suite"
    assert summary["highlights"][0]["label"] == "pytest"


def test_summary_verify_tests_fail_verdict():
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task, merge_test_result={"detected": True, "passed": False}
    )
    assert summary["verdict"] == "fail"


def test_summary_verify_tests_undetected_verdict():
    # No merge_test_result context → undetected (neutral) verdict.
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "undetected"


def test_summary_memo_artifact():
    task = _make_task(
        "memo",
        plan_output={
            "memo": {
                "summary": "Cache strategy summary",
                "reasoning": "Why we picked this cache.",
                "citations": ["task:1", "direction:cache"],
                "tags": ["memo"],
            }
        },
    )
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "memo"
    assert summary["evidence"][0]["value"] == "Cache strategy summary"
    assert summary["highlights"][0]["label"] == "Citations"


def test_summary_memo_without_artifact_is_none():
    task = _make_task("memo", plan_output={})
    assert build_task_evaluation_summary(task) is None


def test_summary_self_audit_verdict_and_evidence():
    task = _make_task(
        "self_audit",
        plan_output=[
            {"system_id": "auth", "score": 45, "recommended_mode": "audit"},
            {"system_id": "billing", "score": 80, "recommended_mode": "ship"},
        ],
    )
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "audit"
    assert summary["evidence"][0]["label"] == "Systems"
    assert summary["highlights"][0]["label"] == "auth"


def test_summary_security_audit_actionable():
    task = _make_task(
        "security_audit",
        plan_output={
            "findings": [
                {"severity": "high", "title": "Missing auth check"},
                {"severity": "low", "title": "Verbose logging"},
            ]
        },
    )
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "actionable"
    assert summary["evidence"][1]["value"] == "1"
    assert summary["highlights"][0]["label"] == "Missing auth check"


def test_summary_findings_clean_when_no_actionable():
    task = _make_task(
        "perf",
        plan_output={"findings": [{"severity": "low", "title": "minor"}]},
    )
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "clean"


def test_summary_unregistered_type_falls_through_to_findings():
    # A type with no dedicated builder still gets a summary via the shared
    # findings fallback when its plan_output carries findings — no new branch.
    task = _make_task(
        "release_audit",
        plan_output={"findings": [{"severity": "critical", "title": "boom"}]},
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "actionable"


def test_summary_type_without_findings_is_none():
    task = _make_task("plan", plan_output={"notes": "nothing structured"})
    assert build_task_evaluation_summary(task) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
