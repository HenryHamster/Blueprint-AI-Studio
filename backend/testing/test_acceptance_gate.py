"""Standalone tests for the code-task acceptance gate and its shared core.

Covers the fail-closed numeric core shared with the compute success-criterion
gate, and the code acceptance evaluation: no-declaration passes (and records
nothing), a declared named_test that is present + verified + touches a changed
seam passes and is recorded, and the three fail-closed cases (test missing,
test-only diff / does-not-touch changed code, unverified merge). Pure in-memory
Task objects, no DB — runs under ``bash scripts/test-backend.sh --standalone``.
"""

import uuid

from app.acceptance_gate import (
    CODE_ACCEPTANCE_VERDICT_KEY,
    CRITERION_OPS,
    acceptance_blocks_success,
    acceptance_is_proven,
    acceptance_was_declared,
    coerce_number,
    criterion_met,
    evaluate_code_acceptance,
    record_code_acceptance_verdict,
)
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.models import Task, TaskStatus


def _make_code_task(
    *,
    acceptance_evidence: dict | None = None,
    changed_files: list[str] | None = None,
    unverified_reason: str | None = None,
    task_type: str = "create",
) -> Task:
    payload: dict = {"type": task_type, "prompt": "do the thing"}
    if acceptance_evidence is not None:
        payload["acceptance_evidence"] = acceptance_evidence
    context: dict = {}
    if changed_files is not None:
        context["_pr_changed_files"] = changed_files
    if unverified_reason is not None:
        context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    return Task(
        project_id=uuid.uuid4(),
        title="code task",
        payloads=[payload],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=context,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared fail-closed numeric core
# ─────────────────────────────────────────────────────────────────────────────


def test_coerce_number_rejects_bool_and_non_numeric():
    assert coerce_number(3) == 3.0
    assert coerce_number(2.5) == 2.5
    assert coerce_number(True) is None
    assert coerce_number(False) is None
    assert coerce_number("5") is None
    assert coerce_number(None) is None


def test_criterion_met_fail_closed():
    assert criterion_met(0.95, ">=", 0.9) is True
    assert criterion_met(0.5, ">=", 0.9) is False
    # Absent / non-numeric value or threshold is never met.
    assert criterion_met(None, ">=", 0.9) is False
    assert criterion_met("0.95", ">=", 0.9) is False
    assert criterion_met(0.95, ">=", None) is False
    # Unrecognized operator is never met.
    assert criterion_met(1, "~=", 1) is False


def test_criterion_ops_are_the_closed_set():
    assert set(CRITERION_OPS) == {">=", "<=", ">", "<", "==", "!="}


# ─────────────────────────────────────────────────────────────────────────────
# No declaration — preserve today's behavior exactly
# ─────────────────────────────────────────────────────────────────────────────


def test_no_declaration_passes_and_records_nothing():
    task = _make_code_task(acceptance_evidence=None, changed_files=["backend/app/x.py"])
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is False
    assert verdict.satisfied is True
    # Recording is a no-op and writes no key, so scoring is unchanged.
    assert record_code_acceptance_verdict(task) is None
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    assert acceptance_blocks_success((task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)) is False


# ─────────────────────────────────────────────────────────────────────────────
# named_test — pass case
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_present_verified_touches_seam_passes_and_records():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,  # real merge-test verdict gated the merge
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.kind == "named_test"
    # A satisfied named_test is the ONLY machine-checked / proven kind.
    assert verdict.proven is True

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None and recorded.satisfied is True
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert stamped["kind"] == "named_test"
    # A satisfied verdict does NOT block success scoring.
    assert acceptance_blocks_success(stamped) is False
    # ...and IS counted as genuinely proven by machine-checked evidence.
    assert acceptance_is_proven(stamped) is True
    assert acceptance_was_declared(stamped) is True


# ─────────────────────────────────────────────────────────────────────────────
# named_test — three fail-closed cases
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_missing_from_changed_files_fails_closed():
    # The claimed test is not among the PR's changed files — never added here.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "not among" in verdict.reason

    record_code_acceptance_verdict(task)
    assert acceptance_blocks_success(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True


def test_named_test_test_only_diff_fails_closed():
    # Test present + verified, but the diff is test-only: no changed production
    # seam for the test to exercise (covers the "does not touch changed code" and
    # the always-true-test-added-in-isolation shapes).
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "test-only" in verdict.reason


def test_named_test_unverified_merge_fails_closed():
    # Test present + touches a real seam, but the merge fell open WITHOUT real
    # test verification, so the named test did not actually run green.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="no_runnable_suite",
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "did not run green" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# observable_behavior / downstream_artifact — checkable subset
# ─────────────────────────────────────────────────────────────────────────────


def test_observable_behavior_with_real_diff_passes():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=["admin-ui/src/components/Banner.tsx"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True


def test_observable_behavior_empty_diff_fails_closed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=[],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "no changed files" in verdict.reason


def test_downstream_artifact_with_real_diff_passes():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-deploy-artifact-registry",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True


# ─────────────────────────────────────────────────────────────────────────────
# acceptance_blocks_success contract
# ─────────────────────────────────────────────────────────────────────────────


def test_acceptance_blocks_success_only_on_explicit_unsatisfied():
    assert acceptance_blocks_success(None) is False
    assert acceptance_blocks_success({"satisfied": True}) is False
    assert acceptance_blocks_success({"satisfied": False}) is True
    # Malformed / non-dict recorded value never blocks (additive, fail-safe).
    assert acceptance_blocks_success("garbage") is False
    assert acceptance_blocks_success({}) is False


# ─────────────────────────────────────────────────────────────────────────────
# proven axis — only a satisfied named_test is machine-checked proven
# ─────────────────────────────────────────────────────────────────────────────


def test_soft_kinds_are_satisfied_but_not_proven():
    # A satisfied observable_behavior / downstream_artifact proves only that a
    # diff shipped — it is satisfied but NOT machine-checked proven.
    for kind, extra in (
        ("observable_behavior", {"description": "the dashboard shows the SHA banner"}),
        ("downstream_artifact", {"artifact_id": "release-deploy-artifact-registry"}),
    ):
        task = _make_code_task(
            acceptance_evidence={"kind": kind, **extra},
            changed_files=["backend/app/x.py"],
        )
        verdict = evaluate_code_acceptance(task)
        assert verdict.satisfied is True
        assert verdict.proven is False
        assert record_code_acceptance_verdict(task).proven is False
        recorded = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        assert recorded["satisfied"] is True
        assert recorded["proven"] is False
        # Declared (so coverage counts it) but NOT proven.
        assert acceptance_was_declared(recorded) is True
        assert acceptance_is_proven(recorded) is False


def test_failed_named_test_is_declared_but_not_proven():
    # A declared-but-unsatisfied named_test (test missing from the diff) is
    # declared, blocks success, and is never proven.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    recorded = record_code_acceptance_verdict(task)
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None and stamped["proven"] is False
    assert acceptance_was_declared(stamped) is True
    assert acceptance_is_proven(stamped) is False
    assert acceptance_blocks_success(stamped) is True


def test_acceptance_was_declared_and_is_proven_contract():
    # Undeclared / legacy / garbage rows are never declared and never proven.
    for bad in (None, "garbage", 123, []):
        assert acceptance_was_declared(bad) is False
        assert acceptance_is_proven(bad) is False
    # An empty dict counts as "declared" (a verdict dict was stamped) but,
    # lacking a proven=True flag, is not proven — fail-safe for legacy verdicts
    # written before the proven key existed.
    assert acceptance_was_declared({}) is True
    assert acceptance_is_proven({}) is False
    # Only an explicit proven=True reads as proven.
    assert acceptance_is_proven({"proven": True}) is True
    assert acceptance_is_proven({"proven": False}) is False
    assert acceptance_is_proven({"satisfied": True}) is False
