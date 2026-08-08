"""Behavior pin for ``ACCEPTANCE_RULE_VERSION``.

Split out of ``test_acceptance_gate.py``: the branch-covering corpus below is
large and self-contained, and the parent module is a chronic size-cap offender
(``testing/module_size_limits.json``), so this suite lives on its own rather
than pushing the parent past its cap. Behavior is unchanged — the helpers it
needs (``_make_code_task``, ``_with_coverage_result``) are imported from the
parent so there is exactly one definition of each.

Two halves:

  * The in-memory digest pin (:func:`_acceptance_rule_behavior_digest`), which
    proves a rule change cannot silently ship without a ``ACCEPTANCE_RULE_VERSION``
    bump — the precondition re-derivation depends on.
  * The DB-backed PROOF (``test_rule_version_bump_rederives_merged_work_during_
    qualification``) that the bump actually re-scores ALREADY-MERGED work at
    qualification time: it runs the real qualifier tally
    (``release_qualifier._compute_acceptance_window_aggregate``) over a version
    whose window holds stale-rule rows and asserts the re-derived verdicts, the
    DURABLE rule-version stamp written back onto the merged tasks, and the
    re-derivation audit keys.

The DB-backed test needs the Postgres-sidecar runner:
``bash scripts/test-backend.sh testing/test_acceptance_gate__rule_version.py``
(not ``--standalone``, which runs a fixed no-DB file list that omits this
module).
"""

import hashlib

import pytest

from app.acceptance_gate import (
    ACCEPTANCE_RULE_VERSION,
    ACCEPTANCE_TEST_CORROBORATED_KEY,
    CODE_ACCEPTANCE_VERDICT_KEY,
    PARENT_DERIVED_CHANGED_FILES_KEY,
    acceptance_input_fingerprint,
    effective_acceptance_verdict,
    evaluate_acceptance,
    record_code_acceptance_verdict,
)
from app.models import Task, TaskStatus
from testing._release_qualifier_testhelpers import _make_project_and_version, rq
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


def test_current_rule_verdict_rederives_when_evaluator_inputs_change():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
    )
    record_code_acceptance_verdict(task)
    frozen = task.context[CODE_ACCEPTANCE_VERDICT_KEY]

    task.context["_backfill_checked_at"] = "later"
    assert effective_acceptance_verdict(task) is frozen

    task.context["_pr_changed_files"].append("backend/testing/test_widget.py")
    task.context["review_loop"] = {
        "verify_tests_acceptance_block": {
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
            "selector": "backend/testing/test_widget.py",
        }
    }
    effective = effective_acceptance_verdict(task)
    assert effective is not frozen
    assert effective["satisfied"] is True
    assert effective["proven"] is True


def test_current_rule_legacy_verdict_without_fingerprint_rederives():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        merge_test_block={"present": True, "ran": True, "status": "ran",
                          "passed": True, "selector": "backend/testing/test_widget.py"},
    )
    legacy = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": "legacy current-rule verdict",
        "rule_version": ACCEPTANCE_RULE_VERSION,
    }
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = legacy

    effective = effective_acceptance_verdict(task)
    assert effective is not legacy
    assert effective["satisfied"] is True
    assert effective["proven"] is True
    assert effective["input_fingerprint"] == acceptance_input_fingerprint(
        task.first_payload, task.context, task.project_id
    )
    assert effective["land_time_verdict"] == legacy


async def test_rule_version_bump_rederives_merged_work_during_qualification(
    db_session,
):
    """PROOF that a rule-version bump re-derives ALREADY-MERGED work at
    QUALIFICATION time — with the stamp recorded durably.

    This is the observable the re-derivation design (design doc c50453ef,
    rungs 3/5/7) exists for: an improvement to the acceptance rules re-scores
    work that already merged, instead of being inert until someone re-runs it.
    It runs the REAL qualifier tally
    (``release_qualifier._compute_acceptance_window_aggregate``) — the exact
    code a qualification drive executes — over a version whose window contains
    three COMPLETED, landed write tasks:

      * ``upgrade_task``  — verdict stamped under the RETIRED rule as
        unsatisfied. The current rule judges the SAME persisted merge record
        proven (test in diff + verified merge + grounded seam), so the tally
        must flip it out of ``declared_unsatisfied`` into ``proven``.
      * ``downgrade_task`` — verdict stamped under the RETIRED rule as proven.
        The current rule judges the same record UNSATISFIED (its production
        seam no longer grounds the declared test — a hard fail, not a soft
        unproven), so re-derivation is honest, not monotonic: the downgrade
        happens and is reported.
      * ``control_task``   — verdict stamped under the CURRENT rule,
        unsatisfied. Trusted verbatim, never re-derived.

    Asserts (a) the tally outcome, (b) the DURABLE re-derived stamp written
    back onto the session-bound merged tasks (rule_version == current, plus the
    audit-honesty ``stamped_at_land_time`` / ``land_time_verdict`` /
    ``rederived_at`` markers), and (c) the window's re-derivation audit keys so
    score movement is inspectable.
    """
    project, version = await _make_project_and_version(db_session)

    def _stale_context(*, changed_files, frozen_satisfied, frozen_proven, title):
        # Shared shape for the two stale-rule rows: a real merge record the
        # current rule can judge, plus a verdict frozen under the RETIRED rule.
        return {
            "pr_merged": True,
            "landed_on_integration": True,
            "_pr_changed_files": changed_files,
            "review_loop": {
                "verify_tests_acceptance_block": {
                    "present": True,
                    "ran": True,
                    "status": "ran",
                    "passed": True,
                    "selector": "backend/testing/test_widget.py",
                },
            },
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": frozen_satisfied,
                "proven": frozen_proven,
                "kind": "named_test",
                "reason": f"{title} under the retired rule",
                "rule_version": ACCEPTANCE_RULE_VERSION - 1,
            },
        }

    upgrade_task = Task(
        project_id=project.id,
        title="stale-unsatisfied-merge",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context=_stale_context(
            changed_files=[
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            frozen_satisfied=False,
            frozen_proven=False,
            title="declared test not among the PR's changed files",
        ),
    )
    downgrade_task = Task(
        project_id=project.id,
        title="stale-proven-merge",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context=_stale_context(
            # consumer.py does NOT semantically ground test_widget.py — the
            # retired rule stamped it proven, the current rule caps it at
            # satisfied/unproven.
            changed_files=[
                "backend/app/consumer.py",
                "backend/testing/test_widget.py",
            ],
            frozen_satisfied=True,
            frozen_proven=True,
            title="proven under the retired rule (no seam-grounding check)",
        ),
    )
    control_task = Task(
        project_id=project.id,
        title="current-rule-unsatisfied",
        payloads=[
            {
                "type": "create",
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_other.py",
                },
            }
        ],
        status=TaskStatus.COMPLETED,
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            "_pr_changed_files": ["backend/app/widget.py"],
            "review_loop": {},
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": False,
                "proven": False,
                "kind": "named_test",
                "reason": "declared named_test ... is not among the PR's changed files",
                "rule_version": ACCEPTANCE_RULE_VERSION,
            },
        },
    )
    control_task.context[CODE_ACCEPTANCE_VERDICT_KEY][
        "input_fingerprint"
    ] = acceptance_input_fingerprint(
        control_task.first_payload,
        control_task.context,
        control_task.project_id,
    )

    # Sanity: the CURRENT rule disagrees with BOTH stale stamps (that is what a
    # bump is for), while the current-rule control is trusted verbatim.
    eff_upgrade = effective_acceptance_verdict(upgrade_task)
    assert eff_upgrade is not upgrade_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert eff_upgrade["satisfied"] is True
    assert eff_upgrade["proven"] is True
    eff_downgrade = effective_acceptance_verdict(downgrade_task)
    assert eff_downgrade is not downgrade_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert eff_downgrade["satisfied"] is False
    assert eff_downgrade["proven"] is False
    assert (
        effective_acceptance_verdict(control_task)
        is control_task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    )

    db_session.add(upgrade_task)
    db_session.add(downgrade_task)
    db_session.add(control_task)
    await db_session.commit()

    agg = await rq._compute_acceptance_window_aggregate(db_session, version)

    # (a) Tally: the upgrade flips into proven; the downgrade and the current-rule
    # control both land in declared_unsatisfied. Without re-derivation the upgrade
    # would ALSO sit in declared_unsatisfied and the fraction would be 0/3 instead
    # of 1/3 — the bump is what changes already-merged work's score.
    assert agg["acceptance_window_total"] == 3
    assert agg["acceptance_window_verified"] == 1
    assert agg["acceptance_window_fraction"] == pytest.approx(1 / 3)
    assert agg["acceptance_window_buckets"]["proven"] == 1
    assert agg["acceptance_window_buckets"]["satisfied_unproven"] == 0
    assert agg["acceptance_window_buckets"]["declared_unsatisfied"] == 2

    # (b) The stamp is recorded DURABLY on the already-merged tasks — not a
    # per-drive throwaway: the re-derived verdict carries the CURRENT rule and
    # the audit-honesty markers, and nests the ORIGINAL land-time verdict.
    up_ctx = upgrade_task.context or {}
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["rule_version"] == (
        ACCEPTANCE_RULE_VERSION
    )
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["satisfied"] is True
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["proven"] is True
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["stamped_at_land_time"] is False
    assert isinstance(up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["rederived_at"], str)
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["rederived_by"] == (
        f"qualifier:v{ACCEPTANCE_RULE_VERSION}"
    )
    assert up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["land_time_verdict"]["satisfied"] is False
    assert (
        up_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["land_time_verdict"]["rule_version"]
        == ACCEPTANCE_RULE_VERSION - 1
    )
    down_ctx = downgrade_task.context or {}
    assert down_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["rule_version"] == (
        ACCEPTANCE_RULE_VERSION
    )
    assert down_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["proven"] is False
    assert down_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["satisfied"] is False
    assert down_ctx[CODE_ACCEPTANCE_VERDICT_KEY]["land_time_verdict"]["proven"] is True
    # A current-rule stamp is untouched — verbatim.
    assert (
        control_task.context[CODE_ACCEPTANCE_VERDICT_KEY]["satisfied"] is False
    )

    # (c) Re-derivation audit keys: how much already-merged work was re-scored,
    # in which direction, and under which retired version it was stamped.
    assert agg["acceptance_window_rederived_count"] == 2
    assert agg["acceptance_window_rederived_upgrades"] == 1
    assert agg["acceptance_window_rederived_downgrades"] == 1
    assert agg["acceptance_window_rule_version"] == ACCEPTANCE_RULE_VERSION
    assert agg["acceptance_window_stale_version_counts"] == {
        ACCEPTANCE_RULE_VERSION - 1: 2
    }
    assert sorted(agg["acceptance_window_rederived_task_ids"]) == sorted(
        [str(upgrade_task.id), str(downgrade_task.id)]
    )
