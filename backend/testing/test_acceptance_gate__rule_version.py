"""Behavior pin for ``ACCEPTANCE_RULE_VERSION``.

Split out of ``test_acceptance_gate.py``: the branch-covering corpus below is
large and self-contained, and the parent module is a chronic size-cap offender
(``testing/module_size_limits.json``), so this suite lives on its own rather
than pushing the parent past its cap. Behavior is unchanged — the helpers it
needs (``_make_code_task``, ``_with_coverage_result``) are imported from the
parent so there is exactly one definition of each.

Standalone in-memory tests; run under
``bash scripts/test-backend.sh --standalone``.
"""

import hashlib

from app.acceptance_gate import (
    ACCEPTANCE_RULE_VERSION,
    ACCEPTANCE_TEST_CORROBORATED_KEY,
    PARENT_DERIVED_CHANGED_FILES_KEY,
    evaluate_acceptance,
)
from app.models import Task
from testing.test_acceptance_gate import _make_code_task, _with_coverage_result


def _acceptance_rule_behavior_corpus() -> list[tuple[str, Task]]:
    """Deterministic acceptance-gate inputs, one entry per decision branch of
    :func:`evaluate_acceptance` (mirrors
    ``test_pure_derivation_core_matches_task_adapter_for_every_kind`` so the pin
    tracks the SAME surface a later re-derivation re-runs). Each entry is
    ``(label, task)`` — the label names the branch it exercises.
    """
    corpus: list[tuple[str, Task]] = [
        # undeclared — no acceptance evidence, non-recorded passing verdict.
        (
            "undeclared",
            _make_code_task(
                acceptance_evidence=None, changed_files=["backend/app/x.py"]
            ),
        ),
        # named_test — present in diff + CI-verified merge + a real merge-test
        # verdict block naming the declared test + grounded seam → proven.
        (
            "named_test_in_diff_verified",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=[
                    "backend/app/widget.py",
                    "backend/testing/test_widget.py",
                ],
                merge_test_block={
                    "present": True,
                    "ran": True,
                    "status": "ran",
                    "passed": True,
                    "selector": "backend/testing/test_widget.py",
                },
            ),
        ),
        # named_test — absent from the diff, no existence evidence → hard fail.
        (
            "named_test_missing_from_diff",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=["backend/app/widget.py"],
            ),
        ),
        # named_test — merge fell open unverified → fail closed.
        (
            "named_test_unverified_merge",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=[
                    "backend/app/widget.py",
                    "backend/testing/test_widget.py",
                ],
                unverified_reason="no CI available on this repo",
            ),
        ),
        # named_test — test present but the diff is test-only → fail closed.
        (
            "named_test_test_only_diff",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=["backend/testing/test_widget.py"],
            ),
        ),
        # named_test — truncated changed-file list, test absent → soft credit.
        (
            "named_test_truncated",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=["backend/app/widget.py"],
            ),
        ),
        # named_test — truncated list but backend-REFUTED existence → hard fail.
        (
            "named_test_truncated_corroborated_absent",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=["backend/app/widget.py"],
            ),
        ),
        # named_test — parent-derived diff match → satisfied, no machine proof.
        (
            "named_test_parent_derived",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=[
                    "backend/app/widget.py",
                    "backend/testing/test_widget.py",
                ],
            ),
        ),
        # named_test — unverified merge but TARGETED green run of the declared
        # test → proven.
        (
            "named_test_targeted_green",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=[
                    "backend/app/widget.py",
                    "backend/testing/test_widget.py",
                ],
                unverified_reason="no CI available on this repo",
                targeted_green=True,
            ),
        ),
        # named_test — NOT modified by the PR but EXISTS at the merge commit,
        # ran green under a verified merge, backend-corroborated → proven.
        (
            "named_test_exists_ran_green_corroborated",
            _make_code_task(
                acceptance_evidence={
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                changed_files=["backend/app/widget.py"],
                merge_test_block={
                    "present": True,
                    "ran": True,
                    "status": "ran",
                    "passed": True,
                    "selector": "backend/testing/test_widget.py",
                },
            ),
        ),
        # downstream_artifact — grounded in the shipped diff → satisfied, no
        # machine proof.
        (
            "downstream_artifact_grounded",
            _make_code_task(
                acceptance_evidence={
                    "kind": "downstream_artifact",
                    "artifact_id": "release-promotion-record",
                },
                changed_files=["backend/app/release_promotion.py"],
            ),
        ),
        # downstream_artifact — unrelated diff → fail closed.
        (
            "downstream_artifact_ungrounded",
            _make_code_task(
                acceptance_evidence={
                    "kind": "downstream_artifact",
                    "artifact_id": "release-promotion-record",
                },
                changed_files=["backend/app/consumer.py"],
            ),
        ),
        # visual — no matching screenshot uploaded → fail closed.
        (
            "visual_no_screenshot",
            _make_code_task(
                acceptance_evidence={
                    "kind": "visual",
                    "component": "MetricsDashboard",
                    "viewport_width": 1280,
                },
                changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
            ),
        ),
        # visual — matching uploaded screenshot → proven.
        (
            "visual_matching_screenshot",
            _make_code_task(
                acceptance_evidence={
                    "kind": "visual",
                    "component": "MetricsDashboard",
                    "viewport_width": 1280,
                },
                changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
            ),
        ),
        # coverage_delta — instrumented coverage meets threshold → proven.
        (
            "coverage_delta_met",
            _with_coverage_result(
                _make_code_task(
                    acceptance_evidence={
                        "kind": "coverage_delta",
                        "threshold": 80,
                    },
                    changed_files=["backend/app/widget.py"],
                ),
                90,
            ),
        ),
        # coverage_delta — coverage below threshold → fail closed.
        (
            "coverage_delta_below_threshold",
            _with_coverage_result(
                _make_code_task(
                    acceptance_evidence={
                        "kind": "coverage_delta",
                        "threshold": 80,
                    },
                    changed_files=["backend/app/widget.py"],
                ),
                50,
            ),
        ),
        # observable_behavior — real diff shipped → satisfied.
        (
            "observable_behavior_real_diff",
            _make_code_task(
                acceptance_evidence={
                    "kind": "observable_behavior",
                    "description": "submits the form",
                },
                changed_files=["backend/app/widget.py"],
            ),
        ),
        # observable_behavior — empty diff → fail closed.
        (
            "observable_behavior_empty_diff",
            _make_code_task(
                acceptance_evidence={
                    "kind": "observable_behavior",
                    "description": "submits the form",
                },
                changed_files=[],
            ),
        ),
        # unknown/future kind — fail closed.
        (
            "unknown_kind",
            _make_code_task(
                acceptance_evidence={"kind": "quantum_leap", "threshold": 1},
                changed_files=["backend/app/widget.py"],
            ),
        ),
    ]

    # Post-process the entries whose branch is shaped by a context flag the
    # factory does not expose directly.
    by_label = dict(corpus)
    by_label["named_test_truncated"].context["_pr_changed_files_truncated"] = True
    by_label["named_test_truncated_corroborated_absent"].context[
        "_pr_changed_files_truncated"
    ] = True
    by_label["named_test_truncated_corroborated_absent"].context[
        ACCEPTANCE_TEST_CORROBORATED_KEY
    ] = False
    by_label["named_test_parent_derived"].context[
        PARENT_DERIVED_CHANGED_FILES_KEY
    ] = True
    visual = by_label["visual_matching_screenshot"]
    visual.context["viewport_failure_screenshot_uri"] = (
        "gs://bucket/viewport-failures/"
        f"{visual.project_id}/MetricsDashboard-1280.png"
    )
    by_label["named_test_exists_ran_green_corroborated"].context[
        ACCEPTANCE_TEST_CORROBORATED_KEY
    ] = True
    return corpus


def _acceptance_rule_behavior_digest(corpus: list[tuple[str, Task]]) -> str:
    """sha256 over each corpus entry's (label, kind, declared, satisfied,
    proven) verdict tuple. ``reason``/``proof_ref`` are excluded so wording
    changes do not churn the pin — only a verdict flip (the thing a version bump
    exists for) changes the digest."""
    digest = hashlib.sha256()
    for label, task in corpus:
        verdict = evaluate_acceptance(
            task.first_payload, task.context, task.project_id
        )
        digest.update(
            f"{label}\x00{verdict.kind}\x00{verdict.declared}\x00"
            f"{verdict.satisfied}\x00{verdict.proven}\x00".encode()
        )
    return digest.hexdigest()


# Registered behavior digests keyed by ACCEPTANCE_RULE_VERSION. The corpus above
# must produce the registered digest under the CURRENT rule; a rule change that
# flips any verdict alters the digest and fails
# ``test_acceptance_rule_version_must_move_with_rule_changes``, forcing the
# version bump re-derivation depends on.
_ACCEPTANCE_RULE_BEHAVIOR_DIGESTS: dict[int, str] = {
    1: "42d52a7f2d622758e02b936ad38f96836d4918c79729d5957886b4396e8006e8",
}


def test_acceptance_rule_version_must_move_with_rule_changes():
    """``ACCEPTANCE_RULE_VERSION`` must move when the acceptance evaluation rule
    changes. Re-derivation (:func:`effective_acceptance_verdict`) only re-scores
    a recorded row whose ``rule_version`` differs from the current constant, so
    a rule change without a bump leaves every already-merged verdict trusted
    verbatim — the re-derivation pass never fires.

    Pins the current rule's verdicts over a branch-covering corpus (every
    ``evaluate_acceptance`` branch) to the golden digest registered for
    ``ACCEPTANCE_RULE_VERSION``. A rule change that flips any verdict alters the
    digest and fails here, forcing a deliberate bump + re-registration. Bump
    ``ACCEPTANCE_RULE_VERSION`` in ``app/acceptance_gate.py`` and add the new
    digest to ``_ACCEPTANCE_RULE_BEHAVIOR_DIGESTS`` (run this test once and copy
    the computed digest printed in the failure message)."""
    corpus = _acceptance_rule_behavior_corpus()
    digest = _acceptance_rule_behavior_digest(corpus)
    registered = _ACCEPTANCE_RULE_BEHAVIOR_DIGESTS.get(ACCEPTANCE_RULE_VERSION)
    assert registered, (
        f"ACCEPTANCE_RULE_VERSION={ACCEPTANCE_RULE_VERSION} has no registered "
        f"behavior digest (computed {digest}). When the acceptance rule changes, "
        f"bump ACCEPTANCE_RULE_VERSION and register the new digest."
    )
    assert digest == registered, (
        f"acceptance rule behavior digest changed under "
        f"ACCEPTANCE_RULE_VERSION={ACCEPTANCE_RULE_VERSION}: registered "
        f"{registered}, computed {digest}. The judging rule changed without "
        f"bumping the version, so re-derivation would NOT re-score already-merged "
        f"work judged under the retired rule. Bump ACCEPTANCE_RULE_VERSION in "
        f"acceptance_gate.py and register the new digest."
    )
