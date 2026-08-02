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


def test_write_merged_spawn_seed_without_terminal_verdict_is_success_legacy():
    # The spawn-time seed is no longer the canonical truth signal. Without a
    # terminal reproduce_verdict, merged scoring falls back to merge semantics.
    ctx = {**_merged_context(), "target_defect_reproduction": "present"}
    task = _make_task("fix", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_merged_indeterminate_spawn_seed_without_terminal_verdict_is_success():
    # An indeterminate spawn seed alone must not hold back a later merged task;
    # only the terminal reproduce_verdict can downgrade success.
    ctx = {**_merged_context(), "target_defect_reproduction": "indeterminate"}
    task = _make_task("fix", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_merged_absent_spawn_seed_without_terminal_verdict_is_success():
    # A historical row that only carries the spawn-time seed keeps bare-merge
    # success until a terminal reproduce verdict is actually recorded.
    ctx = {**_merged_context(), "target_defect_reproduction": "absent"}
    task = _make_task("fix", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)


def test_write_merged_no_verdict_key_is_success_legacy():
    # No recorded verdict (feature create, legacy row) keeps bare-merge success.
    task = _make_task("create", context=_merged_context())
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)


def test_write_unverified_degraded_neutral_regardless_of_verdict():
    # An unverified-degraded merge stays neutral even when the defect reproduced;
    # the degraded-environment gate is independent of the reproduction gate.
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    ctx = {
        **_merged_context(),
        REVIEW_LOOP_KEY: {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"},
        "reproduce_verdict": {"status": "reproduced"},
    }
    task = _make_task("fix", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)


def test_write_success_matches_is_merged_completion_exactly():
    from app.landed_signal import is_merged_completion

    merged = _make_task("refactor", context=_merged_context())
    unmerged = _make_task("refactor", context={})
    assert is_successful_outcome(merged) == is_merged_completion(merged) is True
    assert is_successful_outcome(unmerged) == is_merged_completion(unmerged) is False


def test_write_merged_satisfied_acceptance_is_success():
    # A merged code task whose declared acceptance evidence was SATISFIED keeps
    # its verified-success credit.
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    ctx = {
        **_merged_context(),
        CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": True, "kind": "named_test"},
    }
    task = _make_task("create", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)


def test_write_merged_unsatisfied_acceptance_is_neutral():
    # Declared acceptance evidence that was NOT satisfied (e.g. the named test is
    # absent or ran unverified) holds the merge back from a verified success.
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    ctx = {
        **_merged_context(),
        CODE_ACCEPTANCE_VERDICT_KEY: {"satisfied": False, "kind": "named_test"},
    }
    task = _make_task("create", context=ctx)
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_merged_no_acceptance_key_is_success_legacy():
    # No recorded acceptance verdict (no declaration / legacy row) keeps the
    # bare-merge success exactly as before.
    task = _make_task("create", context=_merged_context())
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)


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
        context={"research_findings_finalized": True},
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


def test_research_surviving_findings_recorded_is_success():
    # When the completion handler recorded surviving findings, the cycle is a
    # success regardless of how many raw titled findings sit in plan_output.
    task = _make_task(
        "research",
        plan_output=[{"title": "fix: do the thing"}],
        context={"research_surviving_findings": 2},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_RESEARCH_QUALITY)
    assert is_successful_outcome(task) is True


def test_research_zero_survivors_is_neutral_despite_titled_findings():
    # Core fix: every titled finding was rejected at ingestion (stale dup /
    # citation absent on HEAD / low actionability). The raw pre-gate plan is not
    # an outcome signal; the cycle produced no work and remains neutral.
    task = _make_task(
        "research",
        plan_output=[
            {"title": "fix: stale", "actionability_grade": "HIGH"},
            {"title": "fix: dup", "actionability_grade": "MEDIUM"},
        ],
        context={"research_surviving_findings": 0},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_RESEARCH_QUALITY)
    assert is_successful_outcome(task) is False


def test_research_unfinalized_legacy_findings_fail_closed_to_neutral():
    task = _make_task("research", plan_output=[{"title": "fix: legacy"}], context={})
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_RESEARCH_QUALITY)


def test_research_finalized_findings_supply_legacy_compatible_success():
    task = _make_task(
        "research",
        plan_output=[{"title": "fix: accepted"}],
        context={"research_findings_finalized": True},
    )
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_RESEARCH_QUALITY)


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
    acceptance_grade: str | None = None,
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
        if acceptance_grade is not None:
            plan_output["knowledge_document"] = {
                "source_kind": "compute_experiment",
                "knowledge_acceptance_evidence": acceptance_grade == "acceptance",
                "compute_acceptance_grade": acceptance_grade,
            }
    return _make_task("compute_experiment", status=status, plan_output=plan_output)


def test_compute_experiment_acceptance_grade_success_is_success():
    task = _make_compute_task("SUCCESS", acceptance_grade="acceptance")
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_COMPUTE_RESULT)


def test_compute_experiment_measured_only_success_is_neutral():
    task = _make_compute_task("SUCCESS")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_COMPUTE_RESULT)


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


def test_summary_verify_tests_absent_acceptance_test_on_passing_suite_is_fail():
    # A green suite that promised a named test which is ABSENT must surface
    # "fail" (the gate blocks at HIGH precedence) and carry the test as evidence.
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "the seam test",
                "selector": "testing/test_seam.py::test_x",
                "present": False,
                "ran": False,
                "status": "absent",
            },
        },
    )
    assert summary["verdict"] == "fail"
    labels = {e["label"]: e["value"] for e in summary["evidence"]}
    assert "Declared acceptance test" in labels
    assert "absent" in labels["Declared acceptance test"]


def test_summary_verify_tests_ran_acceptance_test_stays_pass():
    task = _make_task("verify_tests")
    summary = build_task_evaluation_summary(
        task,
        merge_test_result={
            "detected": True,
            "passed": True,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "the seam test",
                "selector": "testing/test_seam.py::test_x",
                "present": True,
                "ran": True,
                "status": "ran",
            },
        },
    )
    assert summary["verdict"] == "pass"
    labels = {e["label"]: e["value"] for e in summary["evidence"]}
    assert "Declared acceptance test" in labels


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
    assert goal_items == [
        {"label": "Goal", "value": "met: accuracy >= 0.9 (measured 0.95)"}
    ]


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


# --- write/PR task evaluation summary (merge + reproduce verdict) ------------


def test_summary_write_merged_reproduced_is_verified_fix():
    context = _merged_context()
    context["reproduce_verdict"] = {
        "status": "reproduced",
        "reason": "asserted NPE reproduced against HEAD before the fix",
    }
    task = _make_task("fix", context=context)
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "verified_fix"
    assert {"label": "Merge", "value": "landed on integration"} in summary["evidence"]
    assert {"label": "Reproduce verdict", "value": "reproduced"} in summary["evidence"]
    assert summary["highlights"][0]["label"] == "Verdict reason"


def test_summary_write_merged_already_resolved_is_shipped_unproven():
    context = _merged_context()
    context["reproduce_verdict"] = {"status": "already_resolved"}
    task = _make_task("fix", context=context)
    summary = build_task_evaluation_summary(task)
    assert summary["verdict"] == "shipped_unproven"


def test_summary_write_degraded_merge_with_reproduced_verdict_is_shipped_unverified():
    from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY

    context = _merged_context()
    context["reproduce_verdict"] = {"status": "reproduced"}
    context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: "verify_setup_failed"}
    task = _make_task("fix", context=context)
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "shipped_unverified"


def test_summary_write_merged_no_verdict_is_merged():
    task = _make_task("create", context=_merged_context())
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "merged"


def test_summary_write_unmerged_already_resolved_noop_renders_verdict():
    # A no-op completion (no merge) that recorded already_resolved still renders
    # a summary surfacing the verdict so operators see WHY there is no PR.
    task = _make_task(
        "fix",
        context={"reproduce_verdict": {"status": "already_resolved"}},
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "already_resolved"
    assert {"label": "Reproduce verdict", "value": "already_resolved"} in summary[
        "evidence"
    ]


def test_summary_write_in_flight_no_verdict_is_none():
    # A write task that neither merged nor carries a verdict has nothing to
    # summarize — preserves the prior no-summary contract for write types.
    task = _make_task("create", context={})
    assert build_task_evaluation_summary(task) is None


# --- merge_verification_status / is_verified_merge --------------------------


def _merged_with_reason(reason: str | None) -> Task:
    """A merged code-merge completion carrying an optional fall-open reason."""
    context = _merged_context()
    if reason is not None:
        context["review_loop"] = {"unverified_merge_reason": reason}
    return _make_task(KIND_CODE_MERGE, context=context)


def test_merge_verification_status_verified_when_no_reason():
    from app.landed_signal import is_verified_merge, merge_verification_status

    task = _merged_with_reason(None)
    assert merge_verification_status(task) == "verified"
    assert is_verified_merge(task) is True


def test_merge_verification_status_benign_reasons():
    from app.ci_resolution import BENIGN_UNVERIFIED_REASONS
    from app.landed_signal import is_verified_merge, merge_verification_status

    for reason in BENIGN_UNVERIFIED_REASONS:
        task = _merged_with_reason(reason)
        assert merge_verification_status(task) == "unverified_benign", reason
        assert is_verified_merge(task) is False, reason


def test_merge_verification_status_degraded_reasons():
    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS
    from app.landed_signal import is_verified_merge, merge_verification_status

    for reason in DEGRADED_UNVERIFIED_REASONS:
        task = _merged_with_reason(reason)
        assert merge_verification_status(task) == "unverified_degraded", reason
        assert is_verified_merge(task) is False, reason


def test_merge_verification_status_unknown_reason_defaults_benign():
    from app.landed_signal import merge_verification_status

    task = _merged_with_reason("some_future_unclassified_reason")
    assert merge_verification_status(task) == "unverified_benign"


def test_merge_verification_status_non_merged_is_none():
    from app.landed_signal import is_verified_merge, merge_verification_status

    # COMPLETED but never landed.
    completed_unmerged = _make_task(KIND_CODE_MERGE, context={})
    assert merge_verification_status(completed_unmerged) is None
    assert is_verified_merge(completed_unmerged) is False

    # Not COMPLETED even though the (stale) landed flag is present.
    in_progress = _make_task(
        KIND_CODE_MERGE, status=TaskStatus.IN_PROGRESS, context=_merged_context()
    )
    assert merge_verification_status(in_progress) is None
    assert is_verified_merge(in_progress) is False


def test_merge_verification_status_ignores_malformed_review_loop():
    from app.landed_signal import merge_verification_status

    context = _merged_context()
    context["review_loop"] = "not-a-dict"
    task = _make_task(KIND_CODE_MERGE, context=context)
    assert merge_verification_status(task) == "verified"


# ─────────────────────────────────────────────────────────────────────────────
# _evaluate_write: degraded-unverified merge downgrade
# ─────────────────────────────────────────────────────────────────────────────


def _write_merged_with_reason(reason: str | None) -> Task:
    """A merged write-type task carrying an optional fall-open reason."""
    context = _merged_context()
    if reason is not None:
        context["review_loop"] = {"unverified_merge_reason": reason}
    return _make_task("create", context=context)


def test_write_degraded_unverified_merge_is_neutral():
    """A merged PR that fell open for a degraded reason must NOT score as success."""
    from app.ci_resolution import DEGRADED_UNVERIFIED_REASONS

    for reason in DEGRADED_UNVERIFIED_REASONS:
        task = _write_merged_with_reason(reason)
        result = evaluate_task_outcome(task)
        assert result == TaskOutcome("neutral", KIND_CODE_MERGE), (
            f"reason={reason!r} should be neutral, got {result}"
        )
        assert is_successful_outcome(task) is False, reason


def test_write_benign_unverified_merge_is_success():
    """A benign fall-open (no suite, kill-switch) must still score as success."""
    from app.ci_resolution import BENIGN_UNVERIFIED_REASONS

    for reason in BENIGN_UNVERIFIED_REASONS:
        task = _write_merged_with_reason(reason)
        result = evaluate_task_outcome(task)
        assert result == TaskOutcome("success", KIND_CODE_MERGE), (
            f"reason={reason!r} should be success, got {result}"
        )
        assert is_successful_outcome(task) is True, reason


def test_write_verified_merge_is_success():
    """A verified merge (no fall-open reason) must score as success."""
    task = _write_merged_with_reason(None)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


# ─────────────────────────────────────────────────────────────────────────────
# _evaluate_write: reproduce-on-HEAD verdict gating of proven-fix success
# ─────────────────────────────────────────────────────────────────────────────


def _write_merged_with_verdict(status: str | None) -> Task:
    """A merged write-type task carrying an optional reproduce-on-HEAD verdict."""
    context = _merged_context()
    if status is not None:
        context["reproduce_verdict"] = {"status": status}
    return _make_task("fix", context=context)


def test_write_merged_reproduced_verdict_is_success():
    """A merged fix whose defect reproduced on HEAD is a proven fix → success."""
    task = _write_merged_with_verdict("reproduced")
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_terminal_reproduced_verdict_overrides_indeterminate_spawn_seed():
    """The terminal canonical verdict outranks the spawn-time seed."""
    context = _merged_context()
    context["target_defect_reproduction"] = "indeterminate"
    context["reproduce_verdict"] = {"status": "reproduced"}
    task = _make_task("fix", context=context)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_merged_already_resolved_verdict_is_neutral():
    """A merged PR whose defect was already resolved ships code but is NOT a
    proven fix — neutral, so it does not inflate proven-fix success accounting."""
    task = _write_merged_with_verdict("already_resolved")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_terminal_already_resolved_verdict_overrides_present_spawn_seed():
    """A terminal already_resolved verdict keeps success neutral even if the
    spawn-time seed said the defect was present on HEAD."""
    context = _merged_context()
    context["target_defect_reproduction"] = "present"
    context["reproduce_verdict"] = {"status": "already_resolved"}
    task = _make_task("fix", context=context)
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_merged_indeterminate_verdict_is_neutral():
    """A merged fix whose reproduce verdict is indeterminate stays neutral."""
    task = _write_merged_with_verdict("indeterminate")
    assert evaluate_task_outcome(task) == TaskOutcome("neutral", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is False


def test_write_merged_no_verdict_preserves_merge_success():
    """A merged task with NO reproduce verdict (feature/refactor or historical)
    keeps the prior merged-PR == success semantics exactly."""
    task = _write_merged_with_verdict(None)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)
    assert is_successful_outcome(task) is True


def test_write_merged_malformed_verdict_falls_back_to_merge_success():
    """A malformed verdict shape degrades to merge-based scoring (success)."""
    context = _merged_context()
    context["reproduce_verdict"] = "not-a-dict"
    task = _make_task("fix", context=context)
    assert evaluate_task_outcome(task) == TaskOutcome("success", KIND_CODE_MERGE)


def test_reproduce_verdict_status_reader():
    """The canonical reader returns the status, or None for missing/malformed."""
    from app.landed_signal import reproduce_verdict_status

    assert (
        reproduce_verdict_status(_write_merged_with_verdict("reproduced"))
        == "reproduced"
    )
    assert reproduce_verdict_status(_make_task("fix", context={})) is None
    bad = _make_task("fix", context={"reproduce_verdict": {"status": "bogus"}})
    assert reproduce_verdict_status(bad) is None


# ─────────────────────────────────────────────────────────────────────────────
# _compute_first_attempt_succeeded: merge-verification awareness
# ─────────────────────────────────────────────────────────────────────────────


def test_first_attempt_succeeded_degraded_returns_false():
    """A degraded-unverified merge_verification makes first_attempt False."""
    from app.task_completion import _compute_first_attempt_succeeded

    assert (
        _compute_first_attempt_succeeded(
            0, TaskStatus.COMPLETED, merge_verification="unverified_degraded"
        )
        is False
    )
    assert (
        _compute_first_attempt_succeeded(
            1, TaskStatus.COMPLETED, merge_verification="unverified_degraded"
        )
        is False
    )


def test_first_attempt_succeeded_benign_unverified_is_unaffected():
    """Benign-unverified merge does not suppress the flag."""
    from app.task_completion import _compute_first_attempt_succeeded

    assert (
        _compute_first_attempt_succeeded(
            0, TaskStatus.COMPLETED, merge_verification="unverified_benign"
        )
        is True
    )


def test_first_attempt_succeeded_verified_is_unaffected():
    """A verified merge keeps the normal first-attempt semantics."""
    from app.task_completion import _compute_first_attempt_succeeded

    assert (
        _compute_first_attempt_succeeded(
            0, TaskStatus.COMPLETED, merge_verification="verified"
        )
        is True
    )
    assert (
        _compute_first_attempt_succeeded(
            2, TaskStatus.COMPLETED, merge_verification="verified"
        )
        is False
    )


def test_first_attempt_succeeded_none_verification_backward_compat():
    """Passing None merge_verification preserves pre-change semantics."""
    from app.task_completion import _compute_first_attempt_succeeded

    assert (
        _compute_first_attempt_succeeded(
            0, TaskStatus.COMPLETED, merge_verification=None
        )
        is True
    )
    assert (
        _compute_first_attempt_succeeded(
            1, TaskStatus.COMPLETED, merge_verification=None
        )
        is True
    )
    assert (
        _compute_first_attempt_succeeded(
            2, TaskStatus.COMPLETED, merge_verification=None
        )
        is False
    )
    assert (
        _compute_first_attempt_succeeded(0, TaskStatus.FAILED, merge_verification=None)
        is False
    )


# ─────────────────────────────────────────────────────────────────────────────
# Acceptance-evidence surfacing (build_task_evaluation_summary wrapper)
# ─────────────────────────────────────────────────────────────────────────────


def _audit_plan_output() -> dict:
    """A findings-shaped plan_output that yields a non-None base summary."""
    return {"findings": [{"title": "leak", "severity": "high"}]}


def test_summary_acceptance_evidence_absent_is_byte_for_byte_unchanged():
    """No declaration ⇒ summary identical to today's (no extra evidence item)."""
    base = build_task_evaluation_summary(
        _make_task("security_audit", plan_output=_audit_plan_output())
    )
    with_ctx = build_task_evaluation_summary(
        _make_task(
            "security_audit", plan_output=_audit_plan_output(), context={"other": 1}
        )
    )
    assert with_ctx == base
    assert all(e["label"] != "Acceptance evidence" for e in with_ctx["evidence"])


def test_summary_acceptance_evidence_declared_is_appended():
    """A well-formed declaration appends alongside the builder's own evidence."""
    task = _make_task(
        "security_audit",
        plan_output=_audit_plan_output(),
        context={
            "acceptance_evidence": {
                "kind": "test",
                "descriptor": "end-to-end seam test passes",
                "selector": "testing/test_foo.py::test_bar",
            }
        },
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    # Builder's own evidence is preserved, with the contract appended last.
    assert summary["evidence"][0]["label"] == "Findings"
    assert summary["evidence"][-1] == {
        "label": "Acceptance evidence",
        "value": "end-to-end seam test passes",
    }


def test_summary_acceptance_evidence_malformed_is_dropped():
    """A malformed declaration (bad kind) never surfaces and never raises."""
    task = _make_task(
        "security_audit",
        plan_output=_audit_plan_output(),
        context={"acceptance_evidence": {"kind": "screenshot", "descriptor": "nope"}},
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert all(e["label"] != "Acceptance evidence" for e in summary["evidence"])


def test_summary_acceptance_evidence_synthesized_when_no_builder_summary():
    """A write type with no findings still surfaces the promised proof.

    A non-merged write task still gets a minimal ``declared`` summary when it
    promised acceptance evidence, even though the write-summary builder itself
    has no shipped/verdict signal to surface yet.
    """
    task = _make_task(
        "create",
        status=TaskStatus.COMPLETED,
        context={
            "acceptance_evidence": {
                "kind": "behavior",
                "descriptor": "the new endpoint scopes audit to the candidate",
            }
        },
    )
    summary = build_task_evaluation_summary(task)
    assert summary is not None
    assert summary["verdict"] == "declared"
    assert summary["evidence"] == [
        {
            "label": "Acceptance evidence",
            "value": "the new endpoint scopes audit to the candidate",
        }
    ]


def test_summary_acceptance_evidence_absent_no_findings_stays_none():
    """No declaration AND no findings ⇒ summary stays None (unchanged)."""
    task = _make_task("create", status=TaskStatus.COMPLETED, context={})
    assert build_task_evaluation_summary(task) is None


# ─────────────────────────────────────────────────────────────────────────────
# mark_stranded_non_landing: clear stale merge-unblock signals
# ─────────────────────────────────────────────────────────────────────────────


def test_stranded_non_landing_clears_stale_merge_unblock():
    """When marking a rollup stranded, clear any stale pr_merged unblock bit.

    A task marked as stranded (landed_on_integration=false) must NOT carry the
    pr_merged unblock signal, so dependency/unblock readers never treat a
    confirmed non-landing as merge-equivalent success. This test validates that
    mark_stranded_non_landing clears any older pr_merged=true bit (e.g., from a
    prior mark_unblocked_non_merge or mark_landed_on_integration call) when
    recording the definitive negative.
    """
    from app.landed_signal import (
        mark_stranded_non_landing,
        has_merge_unblock_signal,
        STRANDED_LANDING_REASON_KEY,
    )
    from app.blocked_need_resolution import _foundation_shipped

    # Start with a task that has the stale merge-unblock signal (e.g., from a
    # previous mark_unblocked_non_merge or mark_landed_on_integration).
    task = _make_task(
        "create",
        status=TaskStatus.COMPLETED,
        context={
            LANDED_SIGNAL_KEY: True,
            LANDED_ON_INTEGRATION_KEY: True,
        },
    )
    assert has_merge_unblock_signal(task) is True
    assert _foundation_shipped(task) is True

    # Mark the task as stranded (ancestry check proved it did NOT land on integration).
    mark_stranded_non_landing(task, reason="rollup->integration merge not an ancestor")

    # After stranding, the merge-unblock signal must be cleared.
    assert has_merge_unblock_signal(task) is False
    # The stranded reason is recorded durably.
    assert task.context.get(STRANDED_LANDING_REASON_KEY) == (
        "rollup->integration merge not an ancestor"
    )
    # landed_on_integration is False.
    assert task.context.get(LANDED_ON_INTEGRATION_KEY) is False
    # The stale pr_merged bit is NOT present.
    assert LANDED_SIGNAL_KEY not in task.context or task.context.get(
        LANDED_SIGNAL_KEY
    ) is not True
    # As a result, _foundation_shipped returns False: the stranded task
    # no longer acts as shipped and cannot unblock dependents.
    assert _foundation_shipped(task) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
