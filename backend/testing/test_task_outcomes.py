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
    KIND_COMPUTE_RESULT,
    KIND_KNOWLEDGE_DOCUMENT,
    KIND_MEMO_ARTIFACT,
    KIND_READ_ONLY_UNCLASSIFIED,
    KIND_RESEARCH_QUALITY,
    KIND_REVIEW_VERDICT,
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
# Compute-experiment family
# ─────────────────────────────────────────────────────────────────────────────


def _make_compute_task(
    outcome: str | None,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    final_cost_usd: float | None = 1.25,
) -> Task:
    """Build a compute_experiment task carrying a terminal compute_result."""
    plan_output = None
    if outcome is not None:
        plan_output = {
            "compute_result": {
                "outcome": outcome,
                "final_cost_usd": final_cost_usd,
            }
        }
    return _make_task("compute_experiment", status=status, plan_output=plan_output)


def test_compute_experiment_success_is_success():
    task = _make_compute_task("SUCCESS")
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_COMPUTE_RESULT)


def test_compute_experiment_failure_is_failure():
    task = _make_compute_task("FAILURE")
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_COMPUTE_RESULT)


def test_compute_experiment_spend_cap_is_neutral():
    task = _make_compute_task("SPEND_CAP_EXCEEDED")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_COMPUTE_RESULT)


def test_compute_experiment_timed_out_is_neutral():
    task = _make_compute_task("TIMED_OUT")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_COMPUTE_RESULT)


def test_compute_experiment_cancelled_is_neutral():
    task = _make_compute_task("CANCELLED")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_COMPUTE_RESULT)


def test_compute_experiment_missing_result_failed_status_is_failure():
    # No compute_result stored (gate failed it) + FAILED status → failure.
    task = _make_compute_task(None, status=TaskStatus.FAILED)
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_COMPUTE_RESULT)


def test_compute_experiment_missing_result_completed_is_neutral():
    # Completed but no compute_result and not a genuine failure → neutral.
    task = _make_compute_task(None, status=TaskStatus.COMPLETED)
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_COMPUTE_RESULT)


# ─────────────────────────────────────────────────────────────────────────────
# Panel-review family
# ─────────────────────────────────────────────────────────────────────────────


def test_panel_review_approve_verdict_is_success():
    task = _make_task(
        "panel_review",
        plan_output={
            "overall_verdict": "APPROVE",
            "summary": "PR looks good",
            "lenses": {
                "correctness": {"verdict": "pass", "summary": "Logic is correct"},
                "security": {"verdict": "pass", "summary": "No security issues"},
                "style": {"verdict": "pass", "summary": "Code style is good"},
                "testing": {"verdict": "pass", "summary": "Tests are adequate"},
            },
        },
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_REVIEW_VERDICT)
    assert is_successful_outcome(task) is True


def test_panel_review_request_changes_verdict_is_neutral():
    task = _make_task(
        "panel_review",
        plan_output={
            "overall_verdict": "REQUEST_CHANGES",
            "summary": "Needs minor fixes",
            "lenses": {
                "correctness": {"verdict": "fail", "summary": "Logic issue"},
                "security": {"verdict": "pass", "summary": "No security issues"},
                "style": {"verdict": "pass", "summary": "Code style is good"},
                "testing": {"verdict": "pass", "summary": "Tests are adequate"},
            },
        },
    )
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_REVIEW_VERDICT)
    assert is_successful_outcome(task) is False


def test_panel_review_no_verdict_is_neutral():
    # COMPLETED panel_review with no usable verdict → neutral.
    task = _make_task("panel_review", plan_output={})
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_REVIEW_VERDICT)


def test_panel_review_failed_is_failure():
    task = _make_task("panel_review", status=TaskStatus.FAILED, plan_output={})
    assert evaluate_task_outcome(task) == TaskOutcome("failure", KIND_REVIEW_VERDICT)


# ─────────────────────────────────────────────────────────────────────────────
# Web-research / knowledge-document family
# ─────────────────────────────────────────────────────────────────────────────


def _make_web_research_task(
    mirror: dict | None,
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
) -> Task:
    """Build a web_research task carrying the compact knowledge-document mirror."""
    plan_output = None
    if mirror is not None:
        plan_output = {"knowledge_document": mirror}
    return _make_task("web_research", status=status, plan_output=plan_output)


def _knowledge_mirror() -> dict:
    return {
        "title": "How vector indexes work",
        "summary": "An overview of ANN indexing tradeoffs.",
        "topic": "retrieval",
        "tags": ["search", "ann"],
        "citation_count": 5,
    }


def test_web_research_with_document_is_success():
    task = _make_web_research_task(_knowledge_mirror())
    assert evaluate_task_outcome(task) == TaskOutcome(
        "success", KIND_KNOWLEDGE_DOCUMENT
    )
    assert is_successful_outcome(task) is True


def test_web_research_no_mirror_is_neutral():
    task = _make_web_research_task(None, status=TaskStatus.COMPLETED)
    assert evaluate_task_outcome(task) == TaskOutcome(
        "neutral", KIND_KNOWLEDGE_DOCUMENT
    )


def test_web_research_empty_title_is_success():
    task = _make_web_research_task({"title": "  ", "summary": "x"})
    assert evaluate_task_outcome(task) == TaskOutcome(
        "success", KIND_KNOWLEDGE_DOCUMENT
    )


def test_web_research_failed_is_failure():
    task = _make_web_research_task(None, status=TaskStatus.FAILED)
    assert evaluate_task_outcome(task) == TaskOutcome(
        "failure", KIND_KNOWLEDGE_DOCUMENT
    )


def test_web_research_cancelled_is_failure():
    # A genuinely cancelled run fails closed even if a stale mirror were present.
    task = _make_web_research_task(
        _knowledge_mirror(),
        status=TaskStatus.CANCELLED,
    )
    assert evaluate_task_outcome(task) == TaskOutcome(
        "failure", KIND_KNOWLEDGE_DOCUMENT
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


def test_summary_verify_tests_declared_build_failed_on_passing_suite_is_fail():
    # A green unit suite that ALSO broke the operator's declared build must not
    # show a clean "pass" — the gate blocks, so the summary verdict is "fail".
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "declared": True,
                "ecosystem": "node",
                "label": "npm run build (admin-ui)",
                "output": "webpack: build error",
            },
        },
    )
    assert summary["verdict"] == "fail"


def test_summary_verify_tests_declared_build_could_not_run_on_passing_suite_stays_pass():
    # A declared build that could not run does NOT block — summary stays "pass".
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": True,
                "declared": True,
                "label": "npm run build (admin-ui)",
            },
        },
    )
    assert summary["verdict"] == "pass"


def test_summary_verify_tests_generic_probe_fail_on_passing_suite_stays_pass():
    # A GENERIC (non-declared) probe failure does not block a passing suite.
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "label": "tsc",
            },
        },
    )
    assert summary["verdict"] == "pass"


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


def test_summary_compute_experiment_success():
    task = _make_compute_task("SUCCESS", final_cost_usd=2.5)
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "success"
    assert summary["evidence"][0] == {"label": "Outcome", "value": "SUCCESS"}
    assert summary["evidence"][1] == {"label": "Final cost", "value": "$2.5"}


def test_summary_compute_experiment_spend_cap():
    task = _make_compute_task("SPEND_CAP_EXCEEDED", final_cost_usd=5.0)
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "spend_cap_exceeded"
    assert summary["evidence"][0]["value"] == "SPEND_CAP_EXCEEDED"


def test_summary_compute_experiment_without_result_is_none():
    task = _make_compute_task(None)
    assert build_task_evaluation_summary(task) is None


def test_summary_compute_experiment_surfaces_met_goal():
    # A completed experiment that declared and met a goal shows it as a Goal item
    # so the operator can distinguish goal-met from merely-completed.
    task = _make_task(
        "compute_experiment",
        status=TaskStatus.COMPLETED,
        plan_output={
            "compute_result": {
                "outcome": "SUCCESS",
                "final_cost_usd": 1.0,
                "criterion": {
                    "metric": "accuracy",
                    "operator": ">=",
                    "threshold": 0.9,
                    "value": 0.95,
                    "met": True,
                },
            }
        },
    )
    summary = build_task_evaluation_summary(task)
    goal_items = [e for e in summary["evidence"] if e["label"] == "Goal"]
    assert goal_items == [{"label": "Goal", "value": "met: accuracy >= 0.9 (measured 0.95)"}]


def test_summary_web_research_recorded():
    task = _make_web_research_task(_knowledge_mirror())
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "recorded"
    assert summary["evidence"][0] == {
        "label": "Title",
        "value": "How vector indexes work",
    }
    assert summary["evidence"][-1] == {"label": "Citations", "value": "5"}
    assert summary["highlights"][0]["label"] == "Tags"


def test_summary_web_research_without_mirror_is_none():
    task = _make_web_research_task(None)
    assert build_task_evaluation_summary(task) is None


def test_summary_web_research_empty_title_uses_other_evidence():
    task = _make_web_research_task({"title": "  ", "summary": "x"})
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "recorded"
    assert summary["evidence"][0] == {"label": "Summary", "value": "x"}


def _panel_review_verdict_plan(
    overall: str,
    *,
    lenses: dict | None = None,
    issues: list | None = None,
    investigated_files: list | None = None,
) -> dict:
    """A normalized panel_review verdict as stored on plan_output."""
    plan: dict = {
        "overall_verdict": overall,
        "summary": "Synthesized panel verdict",
        "lenses": lenses
        if lenses is not None
        else {
            "correctness": {"verdict": "APPROVE", "summary": "Logic correct", "issues": []},
            "security": {"verdict": "APPROVE", "summary": "No issues", "issues": []},
            "code_quality": {"verdict": "APPROVE", "summary": "Clean", "issues": []},
            "test_coverage": {"verdict": "APPROVE", "summary": "Covered", "issues": []},
        },
        "investigated_files": investigated_files
        if investigated_files is not None
        else ["a.py", "b.py"],
    }
    if issues is not None:
        plan["issues"] = issues
    return plan


def test_summary_panel_review_clean_approve_is_not_none():
    # A clean APPROVE with no issues must still produce a summary — the verdict
    # itself is the signal — never an empty/None result.
    task = _make_task(
        "panel_review", plan_output=_panel_review_verdict_plan("APPROVE")
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "approve"
    assert summary["evidence"][0] == {"label": "Decision", "value": "APPROVE"}
    assert summary["evidence"][1] == {"label": "Lenses", "value": "4/4 passed"}
    assert summary["evidence"][2] == {
        "label": "Issues",
        "value": "0 total (critical: 0, major: 0, minor: 0)",
    }
    assert summary["evidence"][3] == {"label": "Files reviewed", "value": "2"}
    assert {"label": "Correctness", "value": "pass"} in summary["highlights"]
    assert len(summary["highlights"]) == 3  # capped at 3


def test_summary_panel_review_request_changes_counts_all_severities():
    # REQUEST_CHANGES with major/minor issues — severities the generic fallback
    # would drop must be counted honestly here.
    plan = _panel_review_verdict_plan(
        "REQUEST_CHANGES",
        lenses={
            "correctness": {
                "verdict": "REQUEST_CHANGES",
                "summary": "Bug",
                "issues": [],
            },
            "security": {"verdict": "APPROVE", "summary": "ok", "issues": []},
            "code_quality": {"verdict": "APPROVE", "summary": "ok", "issues": []},
            "test_coverage": {"verdict": "APPROVE", "summary": "ok", "issues": []},
        },
        issues=[
            {"severity": "critical", "description": "crash"},
            {"severity": "major", "description": "leak"},
            {"severity": "minor", "description": "nit"},
            {"severity": "minor", "description": "nit2"},
        ],
    )
    task = _make_task("panel_review", plan_output=plan)
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "request_changes"
    assert summary["evidence"][1] == {"label": "Lenses", "value": "3/4 passed"}
    assert summary["evidence"][2] == {
        "label": "Issues",
        "value": "4 total (critical: 1, major: 1, minor: 2)",
    }
    assert {"label": "Correctness", "value": "fail"} in summary["highlights"]


def test_summary_panel_review_counts_lens_issues_without_top_level():
    # When only per-lens issues are present (no top-level deduped list), the
    # count is still honest.
    plan = _panel_review_verdict_plan(
        "REQUEST_CHANGES",
        lenses={
            "correctness": {
                "verdict": "REQUEST_CHANGES",
                "summary": "Bug",
                "issues": [{"severity": "major", "description": "x"}],
            },
            "security": {
                "verdict": "REQUEST_CHANGES",
                "summary": "Risk",
                "issues": [{"severity": "minor", "description": "y"}],
            },
            "code_quality": {"verdict": "APPROVE", "summary": "ok", "issues": []},
            "test_coverage": {"verdict": "APPROVE", "summary": "ok", "issues": []},
        },
    )
    task = _make_task("panel_review", plan_output=plan)
    summary = build_task_evaluation_summary(task)
    assert summary["evidence"][2] == {
        "label": "Issues",
        "value": "2 total (critical: 0, major: 1, minor: 1)",
    }


def test_summary_panel_review_no_verdict_is_none():
    task = _make_task("panel_review", plan_output={})
    assert build_task_evaluation_summary(task) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
