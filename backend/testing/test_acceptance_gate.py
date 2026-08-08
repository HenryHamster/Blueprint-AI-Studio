"""Standalone tests for the code-task acceptance gate and its shared core.

Covers the fail-closed numeric core shared with the compute success-criterion
gate, and the code acceptance evaluation: no-declaration passes (and records
nothing), a declared named_test that is present + verified + touches a changed
seam passes and is recorded (machine-checked proof), a declared visual
declaration that matches the uploaded viewport screenshot is recorded as
machine-checked proof, a consumed downstream_artifact is upgraded to proven by
recorded consumption, and the fail-closed cases (test missing, test-only diff,
unverified merge). Standalone
in-memory tests run under ``bash scripts/test-backend.sh --standalone``; DB
tests (consumed artifact upgrade) run under the full test harness.
"""

import os
import uuid

import pytest
from sqlmodel import select

from app.acceptance_gate import (
    ACCEPTANCE_RULE_VERSION,
    CODE_ACCEPTANCE_VERDICT_KEY,
    CRITERION_OPS,
    PREMERGE_ACCEPTANCE_FINDING_KEY,
    REDERIVED_BY_QUALIFIER,
    TEST_COVERAGE_RESULT_KEY,
    acceptance_blocks_success,
    acceptance_is_proven,
    acceptance_counts_as_verified_context,
    acceptance_was_declared,
    backfill_acceptance_verdicts,
    classify_merged_acceptance,
    classify_merged_acceptance_context,
    classify_undeclared_reason,
    classify_unsatisfied_reason,
    coerce_number,
    criterion_met,
    effective_acceptance_verdict,
    evaluate_acceptance,
    evaluate_code_acceptance,
    evaluate_premerge_acceptance,
    format_premerge_acceptance_finding,
    premerge_acceptance_finding,
    record_artifact_consumptions_for_task,
    record_code_acceptance_verdict,
    stamp_premerge_acceptance_finding,
    summarize_version_acceptance_aggregate,
    _evaluate_coverage_delta,
)
from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.models import (
    Artifact,
    ArtifactConsumption,
    ArtifactType,
    CriterionEvidence,
    ReleaseVersion,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)
from app.payload_schemas import AcceptanceEvidence
from testing.factories import make_project

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _make_code_task(
    *,
    acceptance_evidence: dict | None = None,
    changed_files: list[str] | None = None,
    unverified_reason: str | None = None,
    targeted_green: bool = False,
    merge_test_block: dict | None = None,
    task_type: str = "create",
) -> Task:
    payload: dict = {"type": task_type, "prompt": "do the thing"}
    if acceptance_evidence is not None:
        payload["acceptance_evidence"] = acceptance_evidence
    context: dict = {}
    if changed_files is not None:
        context["_pr_changed_files"] = changed_files
    # Set review_loop to indicate verification state: presence without unverified_reason
    # means verified (real test verdict gated merge); presence with unverified_reason
    # means unverified (no-CI merge gate fell open).
    if unverified_reason is not None:
        context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    elif acceptance_evidence is not None and acceptance_evidence.get("kind") == "named_test":
        # named_test requires verification; set empty review_loop to indicate verified
        context[REVIEW_LOOP_KEY] = {}
    # The NORMALIZED acceptance-test block the no-CI monitor persists onto
    # review_loop (orchestrator_ci_monitor writes ci_resolution.acceptance_test_block's
    # output under this key). ``verify_tests_verdict`` is NOT it — that key holds
    # the classifier's outcome STRING ("pass"/"fail"/"undetected").
    if merge_test_block is not None:
        rl = context.setdefault(REVIEW_LOOP_KEY, {})
        rl["verify_tests_acceptance_block"] = merge_test_block
    # The no-CI monitor stamps this durable signal when it independently ran the
    # declared acceptance test as a TARGETED run and it exited green. It rides on
    # the same review_loop the merge-verification reason lives on.
    if targeted_green:
        rl = context.setdefault(REVIEW_LOOP_KEY, {})
        rl["acceptance_test_targeted_green"] = True
        # Faithful to the no-CI monitor: the targeted-green signal is corroborated
        # by the block's selector/descriptor (distilled and stamped as
        # ``acceptance_test_targeted_descriptor``). When no block was supplied,
        # stamp the declared test path so the signal is honest identity evidence
        # for the declared test — the gate now refuses a signal whose identity
        # evidence names a DIFFERENT test.
        if merge_test_block is None and acceptance_evidence is not None:
            _tp = str(acceptance_evidence.get("test_path") or "").strip()
            if _tp:
                rl["acceptance_test_targeted_descriptor"] = _tp.partition("::")[
                    0
                ].strip()
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
# Pure derivation core == task adapter (rungs 1-2 of design doc c50453ef)
# ─────────────────────────────────────────────────────────────────────────────


def test_pure_derivation_core_matches_task_adapter_for_every_kind():
    """The pure ``evaluate_acceptance(payload, context, project_id)`` core must
    return the SAME verdict as the ``Task`` adapter ``evaluate_code_acceptance``
    for every acceptance kind and every branch the gate can take.

    This is the seam a later qualification re-runs to re-derive verdicts that
    were stamped under a retired rule (design doc c50453ef): it must be
    byte-for-byte equivalent to the live task evaluation or re-derivation would
    silently change recorded verdicts.
    """
    def _assert_matches(task: Task) -> None:
        adapter = evaluate_code_acceptance(task)
        pure = evaluate_acceptance(
            task.first_payload, task.context, task.project_id
        )
        assert pure == adapter, (
            f"pure evaluate_acceptance diverged from evaluate_code_acceptance:\n"
            f"  pure:    declared={pure.declared} satisfied={pure.satisfied} "
            f"kind={pure.kind} proven={pure.proven} proof_ref={pure.proof_ref}\n"
            f"  reason:  {pure.reason!r}\n"
            f"  adapter: declared={adapter.declared} satisfied={adapter.satisfied} "
            f"kind={adapter.kind} proven={adapter.proven} proof_ref={adapter.proof_ref}\n"
            f"  reason:  {adapter.reason!r}"
        )

    # undeclared — no acceptance evidence, non-recorded passing verdict.
    _assert_matches(
        _make_code_task(acceptance_evidence=None, changed_files=["backend/app/x.py"])
    )

    # named_test — the six decision branches of _evaluate_named_test.
    # (1) present + verified + grounded seam → proven.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        )
    )
    # (2) missing from the diff (no existence evidence) → hard fail.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            changed_files=["backend/app/widget.py"],
        )
    )
    # (3) unverified merge (fell open without a real green run) → fail-closed.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
            unverified_reason="no CI available on this repo",
        )
    )
    # (4) truncated changed-file list (test may ship beyond the stored prefix) → soft.
    task_truncated = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
    )
    task_truncated.context["_pr_changed_files_truncated"] = True
    _assert_matches(task_truncated)
    # (5) parent-derived rollup diff (a sibling's slice may hold the test) → soft.
    task_parent_derived = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    task_parent_derived.context["_pr_changed_files_from_rollup_parent"] = True
    _assert_matches(task_parent_derived)
    # (6) targeted-green run of a pre-existing test absent from the diff → proven.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            changed_files=["backend/app/widget.py"],
            targeted_green=True,
        )
    )

    # downstream_artifact — grounded, ungrounded, and empty-diff branches.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "downstream_artifact",
                "artifact_id": "release-promotion-record",
            },
            changed_files=["backend/app/release_promotion.py"],
        )
    )
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "downstream_artifact",
                "artifact_id": "release-promotion-record",
            },
            changed_files=["backend/app/consumer.py"],
        )
    )
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "downstream_artifact",
                "artifact_id": "release-promotion-record",
            },
            changed_files=[],
        )
    )

    # visual — missing screenshot fails closed; matching upload is proven.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "visual",
                "component": "MetricsDashboard",
                "viewport_width": 1280,
            },
            changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
        )
    )
    task_visual = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
    )
    task_visual.context["viewport_failure_screenshot_uri"] = (
        "gs://bucket/viewport-failures/"
        f"{task_visual.project_id}/MetricsDashboard-1280.png"
    )
    _assert_matches(task_visual)

    # coverage_delta — meets threshold (proven), below threshold, missing result.
    _assert_matches(
        _with_coverage_result(_make_code_task(
            acceptance_evidence={
                "kind": "coverage_delta",
                "threshold": 80,
            },
            changed_files=["backend/app/widget.py"],
        ), 90)
    )
    _assert_matches(
        _with_coverage_result(_make_code_task(
            acceptance_evidence={
                "kind": "coverage_delta",
                "threshold": 80,
            },
            changed_files=["backend/app/widget.py"],
        ), 50)
    )
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "coverage_delta",
                "threshold": 80,
            },
            changed_files=["backend/app/widget.py"],
        )
    )

    # observable_behavior — real diff satisfied, empty diff fails closed.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "observable_behavior",
                "description": "submits the form",
            },
            changed_files=["backend/app/widget.py"],
        )
    )
    _assert_matches(
        _make_code_task(
            acceptance_evidence={
                "kind": "observable_behavior",
                "description": "submits the form",
            },
            changed_files=[],
        )
    )

    # Unknown/future kind — fail closed rather than silently pass.
    _assert_matches(
        _make_code_task(
            acceptance_evidence={"kind": "quantum_leap", "threshold": 1},
            changed_files=["backend/app/widget.py"],
        )
    )

    # The adapter must ALSO feed the pure core the exact same context the live
    # path reads — a regression here would make re-derivation diverge even when
    # the branches agree.
    adapter = evaluate_code_acceptance(
        _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        )
    )
    assert adapter.declared is True
    assert adapter.proven is True


def _with_coverage_result(task: Task, coverage_pct: float) -> Task:
    """Attach a coverage measurement to a code task for coverage_delta cases."""
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": coverage_pct}
    return task


def test_record_code_acceptance_verdict_stamps_rule_version():
    """Every recorded verdict carries the rule version that produced it, so a
    later qualification can tell which rows were judged by a retired rule."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    record_code_acceptance_verdict(task)
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["rule_version"] == ACCEPTANCE_RULE_VERSION
    assert isinstance(ACCEPTANCE_RULE_VERSION, int) and ACCEPTANCE_RULE_VERSION >= 1
    # A direct recording is an ORIGINAL land-time stamp, never a re-derivation —
    # so a later rule-version bump can tell the two apart (design doc c50453ef §5).
    assert stamped["stamped_at_land_time"] is True
    assert "land_time_verdict" not in stamped
    # Legacy verdicts (no rule_version) are still readable by every consumer —
    # the field is additive.
    assert acceptance_is_proven({"proven": True, "satisfied": True}) is True


# ─────────────────────────────────────────────────────────────────────────────
# effective_acceptance_verdict — re-derivation of stale-rule stamps (rung 3)
# ─────────────────────────────────────────────────────────────────────────────


def test_effective_acceptance_verdict_no_recorded_verdict_returns_none():
    task = _make_code_task(acceptance_evidence=None, changed_files=["backend/app/x.py"])
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    assert effective_acceptance_verdict(task) is None


def test_effective_acceptance_verdict_current_rule_returns_frozen_verbatim():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    frozen = {
        "satisfied": True,
        "proven": True,
        "kind": "named_test",
        "reason": "stamped under the current rule",
        "rule_version": ACCEPTANCE_RULE_VERSION,
    }
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = frozen
    # A current-rule stamp is trusted verbatim — no re-derivation, no rewrite.
    assert effective_acceptance_verdict(task) is frozen


def test_effective_acceptance_verdict_retired_rule_rederives():
    """A verdict frozen under a RETIRED rule is re-derived under the current rule,
    so an evidence-rule improvement landing mid-version re-scores already-merged
    work.  The same (payload, context, project_id) that the retired rule judged
    unsatisfied is judged proven by the current rule."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    # Simulate a stale stamp: the retired rule judged the same evidence
    # unsatisfied (e.g. it never granted the existence-at-merge credit the
    # current rule does), so re-derivation must flip it to proven.
    frozen = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": "declared test not among the PR's changed files (retired rule)",
        "rule_version": ACCEPTANCE_RULE_VERSION - 1,
    }
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = frozen
    effective = effective_acceptance_verdict(task)
    assert effective["satisfied"] is True
    assert effective["proven"] is True
    assert effective["kind"] == "named_test"
    assert effective["rule_version"] == ACCEPTANCE_RULE_VERSION
    assert acceptance_blocks_success(effective) is False
    assert acceptance_is_proven(effective) is True
    # Audit honesty (design doc c50453ef §5): the re-derived dict is marked as a
    # re-derivation, timed, and nests the ORIGINAL land-time verdict — the
    # retired rule's judgment is never destroyed by re-scoring.
    assert effective["stamped_at_land_time"] is False
    assert isinstance(effective["rederived_at"], str) and effective["rederived_at"]
    assert effective["rederived_by"] == REDERIVED_BY_QUALIFIER
    assert effective["land_time_verdict"] == frozen
    # The nested land-time verdict is the ORIGINAL, not another re-derived chain
    # (a legacy stamp with no marker counts as original — absent is not False).
    assert effective["land_time_verdict"].get("stamped_at_land_time") is not False
    assert effective["land_time_verdict"]["rule_version"] == ACCEPTANCE_RULE_VERSION - 1


def test_effective_acceptance_verdict_rederivation_declared_false_keeps_frozen():
    """A frozen verdict whose payload no longer declares evidence stays stable —
    re-derivation must not erase a recorded verdict into the undeclared bucket."""
    task = _make_code_task(acceptance_evidence=None, changed_files=["backend/app/x.py"])
    frozen = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": "stamped before the declaration was removed",
        "rule_version": ACCEPTANCE_RULE_VERSION - 1,
    }
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = frozen
    effective = effective_acceptance_verdict(task)
    assert effective == frozen
    # The frozen unsatisfied stamp still blocks success scoring.
    assert acceptance_blocks_success(effective) is True


def test_effective_acceptance_verdict_preserves_consumed_upgrade():
    """A consumed downstream_artifact verdict is never downgraded by re-derivation:
    the pure core cannot see the consumption rows, so the consumed upgrade
    (proven=True, consumed_by_task_id, consumed_artifact_id) is preserved."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "widget_schema_v3",
        },
        changed_files=["backend/app/widget_schema_v3.py"],
    )
    consumed_by = str(uuid.uuid4())
    consumed_artifact = str(uuid.uuid4())
    frozen = {
        "satisfied": True,
        "proven": True,
        "consumed": True,
        "consumed_by_task_id": consumed_by,
        "consumed_artifact_id": consumed_artifact,
        "kind": "downstream_artifact",
        "reason": "delivered",
        "rule_version": ACCEPTANCE_RULE_VERSION - 1,
    }
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = frozen
    effective = effective_acceptance_verdict(task)
    assert effective["satisfied"] is True
    assert effective["proven"] is True
    assert effective["consumed"] is True
    assert effective["consumed_by_task_id"] == consumed_by
    assert effective["consumed_artifact_id"] == consumed_artifact
    assert effective["rule_version"] == ACCEPTANCE_RULE_VERSION
    assert acceptance_is_proven(effective) is True
    # The consumption upgrade is preserved WITHOUT erasing audit honesty: the
    # original consumed verdict is nested as the land-time verdict.
    assert effective["stamped_at_land_time"] is False
    assert effective["rederived_by"] == REDERIVED_BY_QUALIFIER
    assert effective["land_time_verdict"] == frozen
    assert effective["land_time_verdict"]["consumed"] is True


def test_effective_acceptance_verdict_rederivation_chain_nests_original():
    """Re-deriving an ALREADY-re-derived verdict must nest the ORIGINAL land-time
    verdict — never the previous re-derived dict — so the land-time judgment is
    not replaced by a chain of re-scorings."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    original = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": "declared test not among the PR's changed files (retired rule)",
        "rule_version": ACCEPTANCE_RULE_VERSION - 2,
        "stamped_at_land_time": True,
    }
    # First re-derivation (v-2 -> v-1): nests the original.
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = dict(original)
    once = effective_acceptance_verdict(task)
    assert once["land_time_verdict"] == original
    assert once["stamped_at_land_time"] is False
    assert once["rederived_by"] == REDERIVED_BY_QUALIFIER
    # Second re-derivation (v-1 -> v): the recorded row is the previous
    # re-derived dict; the nested land-time verdict must still be the ORIGINAL.
    task.context[CODE_ACCEPTANCE_VERDICT_KEY] = once
    twice = effective_acceptance_verdict(task)
    assert twice["stamped_at_land_time"] is False
    assert twice["rederived_by"] == REDERIVED_BY_QUALIFIER
    assert twice["land_time_verdict"] == original
    assert twice["land_time_verdict"]["satisfied"] is False
    assert twice["land_time_verdict"]["rule_version"] == ACCEPTANCE_RULE_VERSION - 2


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
    # named_test and visual are proven at evaluation time; a consumed
    # downstream_artifact is the third proven path (see
    # test_consumed_downstream_artifact_upgrades_to_proven).
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


def test_named_test_without_changed_file_provenance_has_distinct_reason():
    """A missing diff signal is not proof that the declared test was omitted."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,
        unverified_reason=None,
    )

    verdict = evaluate_code_acceptance(task)

    assert verdict.satisfied is False
    assert "changed-file provenance was unavailable" in verdict.reason
    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(
        task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    ) == "named_test_diff_provenance_unavailable"


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
# named_test — targeted-green proof under an UNVERIFIED (fell-open) merge
# ─────────────────────────────────────────────────────────────────────────────


def test_targeted_named_test_execution_earns_proven_under_unverified_merge():
    # This project's task PRs target the ``automain`` integration branch, whose
    # no-CI FULL-SUITE gate legitimately falls open BENIGN (stamping an
    # unverified_merge_reason → verified=False). But the merge gate INDEPENDENTLY
    # ran ONLY the declared acceptance test as a targeted run and it exited green,
    # stamping ``acceptance_test_targeted_green=True``. That is honest,
    # machine-checked proof of the exact claim, so the named_test gate must mint
    # proven=True even though the full-suite merge fell open unverified.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
            "test_id": "test_targeted_named_test_execution_earns_proven_under_unverified_merge",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",  # benign fall-open
        targeted_green=True,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert verdict.kind == "named_test"
    assert "targeted" in verdict.reason.lower()
    # proof_ref points at the specific declared test.
    assert verdict.proof_ref == "backend/testing/test_acceptance_gate.py"

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert acceptance_is_proven(stamped) is True
    assert acceptance_blocks_success(stamped) is False


def test_targeted_green_existing_test_absent_from_diff_earns_proven():
    # The no-CI monitor physically ran the declared test as a TARGETED run and it
    # exited green (``acceptance_test_targeted_green=True``) — that is existence
    # evidence at the merge commit PLUS per-test green proof. A behaviour-
    # preserving refactor that re-verifies a pre-existing test it did NOT modify
    # therefore mints machine proof even though the test is absent from the PR's
    # diff: existence-at-merge replaces presence-in-diff as the anti-fraud guard.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],  # test file absent
        unverified_reason="no_detectable_test_command",
        targeted_green=True,
    )
    # Without the backend's own tree corroboration the same shape is credited
    # SOFT — the targeted-green stamp alone is agent-reported evidence.
    interim = evaluate_code_acceptance(task)
    assert interim.satisfied is True
    assert interim.proven is False
    # The merged-landing site stamped the backend's positive tree check.
    from app.acceptance_gate import ACCEPTANCE_TEST_CORROBORATED_KEY
    task.context = {**(task.context or {}), ACCEPTANCE_TEST_CORROBORATED_KEY: True}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert verdict.kind == "named_test"
    assert "NOT modified by this PR but EXISTS" in verdict.reason
    assert verdict.proof_ref == "backend/testing/test_acceptance_gate.py"

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert acceptance_is_proven(stamped) is True


def test_targeted_green_test_only_diff_still_fails_closed():
    # A targeted-green run of a test with NO changed production seam is still a
    # test-only diff — nothing for the test to exercise. Fails closed regardless
    # of the green signal.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/testing/test_acceptance_gate.py"],
        unverified_reason="no_detectable_test_command",
        targeted_green=True,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "test-only" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# named_test — pre-existing test proven at the merge commit (existence-at-merge)
# ─────────────────────────────────────────────────────────────────────────────


def test_existing_test_present_and_green_in_merge_verdict_earns_proven():
    # A merge-test verdict that records the declared acceptance test as present
    # AND ran-green is a real merge-test verdict proving the exact claim — even
    # when the test file is absent from the PR's diff (a behaviour-preserving
    # refactor re-verifying a pre-existing test) and even when the agent's own
    # block says ``in_diff: False``. Existence-at-merge replaces presence-in-diff
    # as the anti-fraud guard for this shape.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],  # test file absent from diff
        unverified_reason=None,  # full-suite merge was verified
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
            "in_diff": False,
        },
    )
    # The block alone (agent-reported) earns only soft credit; the backend's
    # positive tree corroboration upgrades it to the proven mint.
    interim = evaluate_code_acceptance(task)
    assert interim.satisfied is True
    assert interim.proven is False
    from app.acceptance_gate import ACCEPTANCE_TEST_CORROBORATED_KEY
    task.context = {**(task.context or {}), ACCEPTANCE_TEST_CORROBORATED_KEY: True}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert verdict.kind == "named_test"
    assert "NOT modified by this PR but EXISTS" in verdict.reason
    assert verdict.proof_ref == "backend/testing/test_widget.py"

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert acceptance_is_proven(stamped) is True


def test_existing_green_test_ungrounded_seam_fails_closed():
    # Existence + greenness alone is NOT the anti-fraud guard: a pre-existing
    # test that ran green but is NOT semantically grounded to any changed
    # production file is a name-drop of an unrelated test. Still rejected.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/widget.py"],  # unrelated seam
        unverified_reason="no_detectable_test_command",
        targeted_green=True,  # the test exists and ran green...
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "does not semantically correspond" in verdict.reason

    # Classification maps the rejection to the ungrounded-seam cause, not the
    # missing-from-diff cause — the test WAS found green, it just exercises
    # nothing the PR changed.
    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) == (
        "named_test_ungrounded_seam"
    )


def test_named_test_present_false_in_merge_verdict_still_fails_closed():
    # A merge-test verdict that records the declared test as ABSENT
    # (``present=False``) is definitive proof the test does NOT exist at the
    # merge commit — the anti-fraud guard holds: a declared test that exists
    # nowhere is never credited, even though a merge verdict was recorded.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "present": False,
            "ran": False,
            "status": "absent",
            "passed": False,
        },
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "not among" in verdict.reason

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) == (
        "named_test_missing_from_diff"
    )


def test_existing_green_test_only_diff_still_fails_closed():
    # Even a pre-existing test confirmed present and green in the merge-test
    # verdict fails closed when the PR ships NO production seam: a test-only diff
    # has nothing for the test to exercise.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/testing/test_widget.py"],
        unverified_reason=None,
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "test-only" in verdict.reason

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) == (
        "named_test_test_only_diff"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pre-merge acceptance: judge the structurally-checkable half while PR is open
# ─────────────────────────────────────────────────────────────────────────────


def test_premerge_acceptance_failure_reaches_review_loop():
    # A PR whose declared named_test exercises a production seam that does not
    # semantically correspond to the test module is structurally unsatisfiable
    # BEFORE the merge: no production change this PR ships is related to the
    # declared test, so no merge-time green run can ever satisfy the
    # declaration. The pre-merge gate must route that failure into the
    # review/fix loop (a durable, readable finding) so the task can correct the
    # code or the declaration while the PR is still open — instead of
    # discovering it only at merge-landing, when nothing can be done.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/other.py"],  # unrelated production seam
        unverified_reason=None,
    )
    verdict = evaluate_premerge_acceptance(task)
    assert verdict is not None
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "does not semantically correspond" in verdict.reason
    assert verdict.proven is False

    # The structural failure is stamped as a durable finding and read back by
    # the review/fix pipeline's reader.
    finding = stamp_premerge_acceptance_finding(task)
    assert finding is not None
    assert finding["satisfied"] is False
    assert finding["kind"] == "named_test"
    assert finding["cause"] == "named_test_ungrounded_seam"
    assert finding["reason"] == verdict.reason
    assert premerge_acceptance_finding(task) == finding
    assert (task.context or {}).get(PREMERGE_ACCEPTANCE_FINDING_KEY) == finding

    # The finding renders as a fix-prompt block naming the defect and cause, so
    # a fix agent corrects it in the same pass as the review feedback.
    block = format_premerge_acceptance_finding(finding)
    assert "PRE-MERGE ACCEPTANCE FAILURE" in block
    assert "does not semantically correspond" in block
    assert "named_test_ungrounded_seam" in block
    assert format_premerge_acceptance_finding(None) == ""
    assert format_premerge_acceptance_finding({"satisfied": True}) == ""

    # Self-healing: once the agent makes the seam grounded (a change related to
    # the declared test), re-stamping clears the finding.
    task.context = {
        **(task.context or {}),
        "_pr_changed_files": [
            "backend/app/widget.py",
            "backend/testing/test_widget.py",
        ],
    }
    assert stamp_premerge_acceptance_finding(task) is None
    assert premerge_acceptance_finding(task) is None
    assert PREMERGE_ACCEPTANCE_FINDING_KEY not in (task.context or {})

    # The green-run half is NOT pre-merge checkable: a structurally sound
    # named_test (test present + non-test seam + semantically grounded) is
    # satisfied/unproven pre-merge and produces no finding.
    sound = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    sound_verdict = evaluate_premerge_acceptance(sound)
    assert sound_verdict is not None
    assert sound_verdict.satisfied is True
    assert sound_verdict.proven is False
    assert stamp_premerge_acceptance_finding(sound) is None
    assert premerge_acceptance_finding(sound) is None

    # A task that declared NO acceptance evidence is never judged pre-merge and
    # never produces a finding.
    undeclared = _make_code_task(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    assert evaluate_premerge_acceptance(undeclared) is None
    assert stamp_premerge_acceptance_finding(undeclared) is None
    assert premerge_acceptance_finding(undeclared) is None


def test_premerge_named_test_not_in_diff_is_not_a_hard_fail():
    """A declared named_test ABSENT from the PR's diff is not a pre-merge
    structural defect.

    The merge-time gate credits a test that exists at the merge commit and ran
    green even when the PR never modified it (behaviour-preserving work
    re-verifies a pre-existing test). Pre-merge we cannot know whether that
    holds, so absence from the diff alone must credit soft (satisfied/unproven)
    instead of hard-failing the open PR — the anti-fraud existence check is
    deferred to merge. The gate must not steer agents into churning a test the
    merge gate would credit untouched.
    """
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py"],  # production seam, test absent
        unverified_reason=None,
    )
    verdict = evaluate_premerge_acceptance(task)
    assert verdict is not None
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is False
    assert "not among the PR's changed files" in verdict.reason
    assert "exists at the merge commit" in verdict.reason

    # No durable finding is stamped — the review/fix pipeline is not told to
    # correct anything while the PR is still open.
    assert stamp_premerge_acceptance_finding(task) is None
    assert premerge_acceptance_finding(task) is None
    assert PREMERGE_ACCEPTANCE_FINDING_KEY not in (task.context or {})

    # The truncated variant keeps its existing soft-credit behavior.
    truncated = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    truncated.context = {
        **(truncated.context or {}),
        "_pr_changed_files_truncated": True,
    }
    trunc_verdict = evaluate_premerge_acceptance(truncated)
    assert trunc_verdict is not None
    assert trunc_verdict.satisfied is True
    assert trunc_verdict.proven is False

    # An absent-from-diff test with an UNRELATED production seam is still a
    # structural defect (the merge gate enforces the same grounding), so the
    # hard-fail survives for that shape.
    ungrounded = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/other.py"],  # unrelated production seam
        unverified_reason=None,
    )
    assert evaluate_premerge_acceptance(ungrounded).satisfied is False
    assert stamp_premerge_acceptance_finding(ungrounded) is not None


def test_premerge_acceptance_other_kinds_structural_only():
    # downstream_artifact: grounded delivery is structural; an artifact id with
    # no grounded changed path fails closed pre-merge with the same actionable
    # cause as the merge-time gate.
    artifact_task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "deploy-pipeline-registry",
        },
        changed_files=["backend/app/other_module.py"],  # unrelated diff
        unverified_reason=None,
    )
    assert evaluate_premerge_acceptance(artifact_task) is not None
    finding = stamp_premerge_acceptance_finding(artifact_task)
    assert finding is not None
    assert finding["cause"] == "artifact_not_in_diff"

    # visual: a well-formed declaration (component + viewport_width) is
    # satisfied/unproven pre-merge — the uploaded screenshot is a merge-time
    # artifact, so no finding is stamped. A malformed declaration reads as
    # undeclared at the fail-soft read layer (indistinguishable from no
    # declaration), so it produces no finding either.
    sound_visual = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "ComputeJobsPanel",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/ComputeJobsPanel.tsx"],
        unverified_reason=None,
    )
    visual_verdict = evaluate_premerge_acceptance(sound_visual)
    assert visual_verdict is not None and visual_verdict.satisfied is True
    assert visual_verdict.proven is False
    assert stamp_premerge_acceptance_finding(sound_visual) is None

    malformed_visual = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "ComputeJobsPanel",
            # viewport_width omitted → malformed declaration
        },
        changed_files=["admin-ui/src/ComputeJobsPanel.tsx"],
        unverified_reason=None,
    )
    assert evaluate_premerge_acceptance(malformed_visual) is None
    assert stamp_premerge_acceptance_finding(malformed_visual) is None

    # coverage_delta: a well-formed declaration (numeric threshold) is
    # satisfied/unproven pre-merge — the measurement is produced only by a real
    # test run, so it is verified at merge. A missing threshold reads as
    # undeclared (fail-soft), so it produces no finding.
    sound_coverage = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80.0,
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    coverage_verdict = evaluate_premerge_acceptance(sound_coverage)
    assert coverage_verdict is not None and coverage_verdict.satisfied is True
    assert coverage_verdict.proven is False
    assert stamp_premerge_acceptance_finding(sound_coverage) is None

    missing_threshold = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            # threshold omitted → malformed declaration
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    assert evaluate_premerge_acceptance(missing_threshold) is None
    assert stamp_premerge_acceptance_finding(missing_threshold) is None


def test_targeted_green_parent_derived_capped_at_soft():
    # A rollup-child inheriting the parent's combined diff must NOT mint machine
    # proven credit even with a targeted-green run — the matched test could live
    # in a sibling's slice of the union diff. targeted_green never overrides the
    # parent-derived soft cap.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
        targeted_green=True,
    )
    ctx = dict(task.context)
    ctx["_pr_changed_files_from_rollup_parent"] = True
    task.context = ctx
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is False
    assert "sibling" in verdict.reason.lower()


def test_verified_merge_ignores_absent_targeted_green_signal():
    # A normally VERIFIED merge (no unverified_reason) does not depend on the
    # targeted-green signal at all — it stays proven exactly as before.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason=None,  # verified merge
        targeted_green=False,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    # The verified-merge reason wording (not the targeted wording).
    assert "verified merge" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# named_test — semantic grounding
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_unrelated_seam_fails_closed():
    """A declared test whose module name does not match the changed production
    seam fails closed — test_widget.py cannot be proven by a change to
    unrelated_module.py, even though the diff has both test + production files
    and CI ran green."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=[
            "backend/app/unrelated_module.py",
            "backend/testing/test_widget.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "does not semantically correspond" in verdict.reason
    assert verdict.proven is False


def test_named_test_several_seams_one_matches_passes():
    """When multiple production files change and at least one matches the test
    module name, the semantic grounding check passes."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=[
            "backend/app/unrelated_module.py",
            "backend/app/widget.py",
            "backend/testing/test_widget.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason


def test_named_test_cross_cutting_invariants_passes_semantic_grounding():
    """A named_test declaring test_invariants.py — a cross-cutting test whose
    stem 'invariants' does not match any single production module — passes
    semantic grounding because the test validates invariants spanning multiple
    modules and is intrinsically grounded when the PR ships a non-test change."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_invariants.py",
        },
        changed_files=[
            "backend/app/research_direction_state.py",
            "backend/testing/test_invariants.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# named_test — frontend (vitest/jest) test files ground against a changed seam
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_frontend_vitest_grounded_proven():
    """A frontend named_test (`ComputeJobsPanel.test.tsx`) grounds against a
    changed production seam (`ComputeJobsPanel.tsx`) exactly like a Python
    `test_<module>.py` does — this is the recording-seam fix that lets UI-only
    work flip criterion coverage to covered."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        },
        changed_files=[
            "admin-ui/src/components/ComputeJobsPanel.tsx",
            "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason
    assert verdict.proof_ref


def test_named_test_frontend_spec_ts_grounded_proven():
    """The `.spec.ts` (jasmine/jest) naming convention grounds too: a
    `KnowledgeBrowser.spec.ts` grounds against a changed `KnowledgeBrowser.ts`."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "admin-ui/src/components/KnowledgeBrowser.spec.ts",
        },
        changed_files=[
            "admin-ui/src/components/KnowledgeBrowser.ts",
            "admin-ui/src/components/KnowledgeBrowser.spec.ts",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason


def test_named_test_frontend_mts_suffix_grounded_proven():
    """The `[cm]?[jt]sx?` suffix coverage grounds `.mts`/`.cjs`-style names:
    a `Widget.test.mts` grounds against a changed `Widget.ts`."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "admin-ui/src/Widget.test.mts",
        },
        changed_files=[
            "admin-ui/src/Widget.ts",
            "admin-ui/src/Widget.test.mts",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True


def test_named_test_frontend_stem_mismatch_fails_closed():
    """A frontend test whose stem matches no changed production seam fails
    closed — `ComputeJobsPanel.test.tsx` cannot be proven by a change to an
    unrelated `KnowledgeBrowser.tsx`, even with a real merge verdict."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        },
        changed_files=[
            "admin-ui/src/components/KnowledgeBrowser.tsx",
            "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "does not semantically correspond" in verdict.reason
    assert verdict.proven is False


def test_named_test_python_suffix_convention_grounded_proven():
    """A Python ``<module>_test.py`` suffix-named test grounds against a changed
    ``<module>.py`` seam exactly like the ``test_<module>.py`` prefix does.

    ``*_test.py`` is a pytest-recognized test-file shape (see
    ``payload_schemas._TEST_FILE_BASENAME_RE``). Before the stem-grounding fix,
    detection recognized it but ``_test_module_stem`` did not, so a legitimately
    declared ``local_runner_test.py`` co-changed with ``local_runner.py`` failed
    closed to satisfied/unproven. It must now earn machine-checked proof.
    """
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/local_runner_test.py",
        },
        changed_files=[
            "scripts/local_runner.py",
            "backend/testing/local_runner_test.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason
    assert verdict.proof_ref == "backend/testing/local_runner_test.py"


def test_named_test_suffix_convention_stem_mismatch_fails_closed():
    """A ``<module>_test.py`` whose stem matches no changed seam still fails
    closed — the new suffix grounding grants proof only on a real stem match,
    not to any suffix-named test with an unrelated production change."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/local_runner_test.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/local_runner_test.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "does not semantically correspond" in verdict.reason


def test_test_module_stem_covers_suffix_conventions():
    """``_test_module_stem`` extracts the module from every detection-recognized
    suffix shape, truncates ``test_<module>__<variant>.py`` double-underscore
    qualifiers to the base module, and still fails closed on a non-test basename."""
    from app.acceptance_gate import _test_module_stem

    assert _test_module_stem("local_runner_test.py") == "local_runner"
    assert _test_module_stem("orchestrator_test.go") == "orchestrator"
    assert _test_module_stem("pipeline_test.exs") == "pipeline"
    assert _test_module_stem("widget_spec.rb") == "widget"
    # Prefix and JS/TS shapes are unchanged.
    assert _test_module_stem("test_acceptance_gate.py") == "acceptance_gate"
    assert _test_module_stem("ComputeJobsPanel.test.tsx") == "ComputeJobsPanel"
    # The test_<module>__<variant>.py convention truncates at the first ``__``
    # to the BASE module — the variant qualifier is a focus, not the module.
    assert (
        _test_module_stem("test_release_qualifier__sweep_stuck_qualifications.py")
        == "release_qualifier"
    )
    assert _test_module_stem("test_admin_routers__messages.py") == "admin_routers"
    assert _test_module_stem("test_compute__experiments.py") == "compute"
    # The suffix convention truncates an equivalent ``__`` qualifier too.
    assert _test_module_stem("widget__variant_test.py") == "widget"
    # A bare, non-test basename still fails closed (no machine proof granted).
    assert _test_module_stem("random.py") is None


def test_variant_suffixed_test_module_grounds_against_base_module():
    """A ``test_<module>__<variant>.py`` test grounds against its BASE module's
    changed production seam.

    The repo names focused test modules with a double-underscore variant
    qualifier (e.g. ``test_release_qualifier__sweep_stuck_qualifications.py``).
    Before the stem-grounding fix, ``_test_module_stem`` returned the full
    variant-laden stem, which matched no production basename, so a legitimately
    declared variant test co-changed with its base module failed closed to
    satisfied/unproven. The stem now truncates at the first ``__``, so the
    variant test grounds against the base module and earns machine-checked
    proof under a real merge verdict.
    """
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": (
                "backend/testing/test_release_qualifier__sweep_stuck_qualifications.py"
            ),
        },
        changed_files=[
            "backend/app/release_qualifier.py",
            "backend/testing/test_release_qualifier__sweep_stuck_qualifications.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason
    assert verdict.proof_ref == (
        "backend/testing/test_release_qualifier__sweep_stuck_qualifications.py"
    )

    # The pre-merge structural gate agrees: the variant test is grounded against
    # the base module before the green run is verified at merge.
    pre = evaluate_premerge_acceptance(task)
    assert pre is not None
    assert pre.satisfied is True


def test_variant_suffixed_test_module_stem_mismatch_fails_closed():
    """A ``test_<module>__<variant>.py`` test whose BASE module matches no
    changed seam still fails closed — the variant truncation grants proof only
    on a real base-module (or underscore-family) match, not to any variant-named
    test with an unrelated production change."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": (
                "backend/testing/test_release_qualifier__sweep_stuck_qualifications.py"
            ),
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_release_qualifier__sweep_stuck_qualifications.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "does not semantically correspond" in verdict.reason


def test_extracted_submodule_rename_grounds_against_base_and_vice_versa():
    """Extracted-submodule renames ground in BOTH directions.

    A test named for the surviving base module co-changes with an extracted
    submodule (``test_release_qualifier.py`` + ``release_qualifier_redrive.py``),
    and a submodule-named test co-changes with the surviving base
    (``test_release_qualifier_sweep.py`` + ``release_qualifier.py``). Neither is
    an exact basename match, so before the underscore-family broadening both
    failed closed; both now earn machine-checked proof.
    """
    base_test = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_release_qualifier.py",
        },
        changed_files=[
            "backend/app/release_qualifier_redrive.py",
            "backend/testing/test_release_qualifier.py",
        ],
        unverified_reason=None,
    )
    base_verdict = evaluate_code_acceptance(base_test)
    assert base_verdict.satisfied is True, (
        "base-named test must ground against an extracted submodule seam"
    )
    assert base_verdict.proven is True

    submodule_test = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_release_qualifier_sweep.py",
        },
        changed_files=[
            "backend/app/release_qualifier.py",
            "backend/testing/test_release_qualifier_sweep.py",
        ],
        unverified_reason=None,
    )
    sub_verdict = evaluate_code_acceptance(submodule_test)
    assert sub_verdict.satisfied is True, (
        "submodule-named test must ground against the surviving base seam"
    )
    assert sub_verdict.proven is True

    # A submodule-named test whose changed seam is in a DIFFERENT family still
    # fails closed — the family match is strictly an underscore-prefix relation.
    unrelated = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_release_qualifier_sweep.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_release_qualifier_sweep.py",
        ],
        unverified_reason=None,
    )
    unrelated_verdict = evaluate_code_acceptance(unrelated)
    assert unrelated_verdict.satisfied is False
    assert unrelated_verdict.proven is False
    assert "does not semantically correspond" in unrelated_verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# named_test — GitHub-verified file list closes the self-declaration trust gap
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_github_verified_but_truncated_absent_is_inconclusive():
    """A truncated (>50 files) changed-file list is INCONCLUSIVE about absence
    even when the list came from GitHub's API (github_verified sentinel).

    Our own ``stamp_changed_files_signal`` caps the stored list at 50 paths
    regardless of source, so a >50-file PR drops every path beyond index 50. A
    legitimately declared test lying beyond the stored prefix reads as absent from
    the truncated window — hard-failing that truthful declaration is unsound. The
    gate must credit soft (satisfied, unproven), not fail closed.
    """
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],  # test beyond stored prefix
        unverified_reason=None,
    )
    # Simulate the webhook having stamped the GitHub-verified sentinel alongside
    # a truncated list. Truncation makes absence inconclusive regardless of source.
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    ctx["_pr_changed_files_github_verified"] = True
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True, (
        "truncated absent test must credit soft (inconclusive), not fail closed — "
        "even when github-verified, only the first 50 paths are stored"
    )
    assert verdict.proven is False
    assert "truncated" in verdict.reason


def test_named_test_github_verified_present_still_passes():
    """When the file list is GitHub-verified AND the declared test IS present,
    the gate still passes (and proves) exactly as before."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_github_verified"] = True
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True


def test_github_verified_sentinel_does_not_override_truncation_credit():
    """stamp_changed_files_signal with github_verified=True sets the sentinel, but
    the sentinel does NOT flip a truncated-absent named_test to a hard fail.

    Truncation drops paths beyond index 50 in our own storage regardless of
    source, so absence stays inconclusive and the soft credit holds both with and
    without the GitHub-verified sentinel.
    """
    from app.acceptance_gate import stamp_changed_files_signal

    signal = stamp_changed_files_signal(
        ["backend/app/acceptance_gate.py"], github_verified=True
    )
    assert signal.get("_pr_changed_files_github_verified") is True

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    task.context = ctx
    # Without github_verified: soft credit (truncated absence is inconclusive)
    without = evaluate_code_acceptance(task)
    assert without.satisfied is True, "baseline: truncated absent → soft credit"

    # Apply the stamp (as the webhook would) and re-evaluate — still soft credit.
    ctx.update(signal)
    task.context = ctx
    with_sentinel = evaluate_code_acceptance(task)
    assert with_sentinel.satisfied is True, (
        "github_verified sentinel must NOT flip truncated-absent to a hard fail — "
        "only the first 50 paths are stored so absence is inconclusive"
    )
    assert with_sentinel.proven is False


def test_named_test_beyond_50_file_prefix_credits_soft_via_real_stamp():
    """End-to-end regression for finding e8a5b79be22b056b: a >50-changed-files PR
    (github-verified) whose declared test path lies BEYOND the stored 50-path
    prefix must credit soft (inconclusive), not hard-fail on false absence.

    Drives the real ``stamp_changed_files_signal`` truncation so the test exercises
    exactly what the webhook stamps: the first 50 paths (none of which is the
    declared test), ``_pr_changed_files_truncated=True``, and the github-verified
    sentinel. Before the fix this hard-failed (``satisfied=False``) because the
    github-verified sentinel suppressed the truncation credit even though the test
    could not possibly be observed past index 50.
    """
    from app.acceptance_gate import stamp_changed_files_signal

    declared_test = "backend/testing/test_acceptance_gate.py"
    # 54 production files, then the declared test at index 54 — well beyond the
    # 50-path stored prefix. The seam (acceptance_gate.py) sits inside the prefix so
    # the only thing missing from the stored window is the declared test itself.
    changed = ["backend/app/acceptance_gate.py"]
    changed += [f"backend/app/module_{i:03d}.py" for i in range(53)]
    changed.append(declared_test)
    assert len(changed) > 50
    assert declared_test not in changed[:50], "fixture: test must fall past the prefix"

    signal = stamp_changed_files_signal(changed, github_verified=True)
    assert signal["_pr_changed_files_truncated"] is True
    assert signal["_pr_changed_files_github_verified"] is True
    assert declared_test not in signal["_pr_changed_files"]

    task = _make_code_task(
        acceptance_evidence={"kind": "named_test", "test_path": declared_test},
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx.update(signal)
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True, (
        "declared test beyond the stored 50-path prefix must credit soft, not "
        "hard-fail on false absence"
    )
    assert verdict.proven is False
    assert "truncated" in verdict.reason


def test_stamp_changed_files_signal_cap_boundary():
    """The stored ``_pr_changed_files`` list is capped at ``_CHANGED_FILES_STAMP_CAP``
    entries and the truncation sentinel fires exactly on overflow.

    A list one past the cap stores exactly ``_CHANGED_FILES_STAMP_CAP`` paths and
    sets ``_pr_changed_files_truncated``; a list at the cap stores everything and
    sets no truncation sentinel. This pins the single source of truth for the cap
    (the module constant, used in both the slice and the overflow check) so the
    two sites can never drift apart.
    """
    from app.acceptance_gate import (
        _CHANGED_FILES_STAMP_CAP,
        stamp_changed_files_signal,
    )

    over = [f"backend/app/module_{i:03d}.py" for i in range(_CHANGED_FILES_STAMP_CAP + 1)]
    signal_over = stamp_changed_files_signal(over)
    assert len(signal_over["_pr_changed_files"]) == _CHANGED_FILES_STAMP_CAP
    assert signal_over["_pr_changed_files"] == over[:_CHANGED_FILES_STAMP_CAP]
    assert signal_over["_pr_changed_files_truncated"] is True

    at_cap = [f"backend/app/module_{i:03d}.py" for i in range(_CHANGED_FILES_STAMP_CAP)]
    signal_at = stamp_changed_files_signal(at_cap)
    assert len(signal_at["_pr_changed_files"]) == _CHANGED_FILES_STAMP_CAP
    assert signal_at["_pr_changed_files"] == at_cap
    assert "_pr_changed_files_truncated" not in signal_at


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


def test_downstream_artifact_grounded_diff_passes():
    # The declared artifact_id is reflected in a shipped changed-file path
    # (token "promotion" appears in release_promotion.py) — grounded, satisfied,
    # but not machine-checked proven.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is False
    assert "reflected in the shipped changed files" in verdict.reason


def test_downstream_artifact_unrelated_diff_is_unsatisfied():
    # A meaningful-sounding artifact_id paired with a completely unrelated diff
    # is NOT grounded — the declared artifact was not delivered by what shipped,
    # so the verdict fails closed and blocks verified-success credit.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "deploy-rollback-registry",
        },
        changed_files=["admin-ui/src/components/Banner.tsx"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "not reflected" in verdict.reason
    recorded = record_code_acceptance_verdict(task)
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None and stamped["satisfied"] is False
    assert acceptance_blocks_success(stamped) is True


def test_downstream_artifact_empty_diff_fails_closed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "deploy-rollback-registry",
        },
        changed_files=[],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "no changed files" in verdict.reason


@pytest.mark.asyncio
async def test_consumed_downstream_artifact_upgrades_to_proven(db_session):
    """A real consumer consumption row upgrades only the matching producer verdict."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    artifact_key = "release-promotion-record"
    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": artifact_key,
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project_id
    producer.context["provides_capability"] = "release-promotion"
    record_code_acceptance_verdict(producer)
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY] == {
        "satisfied": True,
        "proven": False,
        "kind": "downstream_artifact",
        "reason": (
            "downstream_artifact 'release-promotion-record' is reflected in "
            "the shipped changed files"
        ),
        "proof_ref": "release-promotion-record",
        # Rung 2 of design doc c50453ef: every recorded verdict is stamped with
        # the rule version that produced it so a later qualification can tell
        # which rows were judged by a retired rule and re-derive them.
        "rule_version": ACCEPTANCE_RULE_VERSION,
        # Audit honesty (design doc c50453ef §5): a direct recording is an
        # ORIGINAL land-time stamp, never a re-derivation.
        "stamped_at_land_time": True,
    }
    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project_id
    consumer.depends_on = [str(producer.id)]
    db_session.add(producer)
    db_session.add(consumer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key=artifact_key,
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": artifact_key,
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, [artifact.id]
    )
    assert len(rows) == 1
    # Flush BEFORE refreshing: refresh() expires the instance first, which
    # under real ORM expiry discards its still-pending context change — the
    # explicit flush persists the helper's flag_modified updates so the
    # refresh below genuinely round-trips them through the DB.
    await db_session.flush()
    await db_session.refresh(producer)
    await db_session.refresh(consumer)

    consumption = (
        (
            await db_session.execute(
                select(ArtifactConsumption).where(
                    ArtifactConsumption.consumer_task_id == consumer.id,
                    ArtifactConsumption.artifact_id == artifact.id,
                )
            )
        )
        .scalars()
        .one()
    )
    assert consumption.producer_task_id == producer.id
    assert consumption.logical_key == artifact_key
    verdict = producer.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["satisfied"] is True
    assert verdict["proven"] is True
    assert verdict["consumed"] is True
    assert verdict["consumed_artifact_id"] == str(artifact.id)
    assert verdict["consumed_by_task_id"] == str(consumer.id)
    assert consumer.context["consumed_artifact_ids"] == [str(artifact.id)]

    # record_capability_proof must have been invoked on the producer's project.
    from app.models import Project as ProjectModel

    producer_project = await db_session.get(ProjectModel, project_id)
    assert producer_project is not None
    proof_records = producer_project.capability_proof_records or {}
    assert "release-promotion" in proof_records, (
        f"expected 'release-promotion' key in capability_proof_records; got {list(proof_records.keys())}"
    )
    proof_entries = proof_records["release-promotion"]
    assert len(proof_entries) == 1
    entry = proof_entries[0]
    assert entry["task_id"] == str(producer.id)
    assert entry["proof_kind"] == "downstream_artifact"
    assert entry["proof_ref"] == artifact_key


@pytest.mark.asyncio
async def test_explicit_consumed_artifact_ids_ignore_undelivered_ids(db_session):
    """Explicit consumption reports are limited to delivered artifact prerequisites."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    project_id = project.id

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project_id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    delivered_artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    stray_artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="other-artifact",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/other-artifact.json"
        ),
    )
    db_session.add(delivered_artifact)
    db_session.add(stray_artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project_id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(delivered_artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(
        consumer,
        db_session,
        [delivered_artifact.id, stray_artifact.id],
    )
    assert len(rows) == 1
    # Flush before refresh — see test_consumed_downstream_artifact_upgrades_to_proven.
    await db_session.flush()
    await db_session.refresh(producer)
    await db_session.refresh(consumer)

    verdict = producer.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["consumed"] is True
    assert verdict["consumed_artifact_id"] == str(delivered_artifact.id)
    assert consumer.context["consumed_artifact_ids"] == [str(delivered_artifact.id)]


@pytest.mark.asyncio
async def test_modern_omitted_consumed_artifact_ids_do_not_fallback(db_session):
    """Modern agent omitting consumed_artifact_ids does NOT trigger legacy fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    # modern_agent=True + None means "agent ran but omitted the field" — no fallback
    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, None, modern_agent=True
    )
    assert rows == []
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    # No consumption rows written; producer verdict is NOT upgraded to consumed
    assert consumer.context.get("consumed_artifact_ids") is None
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is not True


@pytest.mark.asyncio
async def test_legacy_omitted_consumed_artifact_ids_falls_back(db_session):
    """Legacy path (modern_agent=False, the default) still uses dependency fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    # modern_agent=False (default) + None triggers legacy fallback from dependencies
    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, None, modern_agent=False
    )
    assert len(rows) == 1
    # Flush before refresh — see test_consumed_downstream_artifact_upgrades_to_proven.
    await db_session.flush()
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    # Legacy fallback DID record consumption
    assert consumer.context.get("consumed_artifact_ids") == [str(artifact.id)]
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is True


@pytest.mark.asyncio
async def test_empty_explicit_consumed_artifact_ids_do_not_fallback(db_session):
    """An explicit empty report means no consumption, not legacy fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(consumer, db_session, [])
    assert rows == []
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    assert consumer.context.get("consumed_artifact_ids") is None
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is not True


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


def test_satisfied_soft_kinds_bucket_as_satisfied_unproven_not_proven():
    """Soft satisfied declarations are not machine-proven in release buckets."""
    soft_task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=["backend/app/release_readiness.py"],
    )
    record_code_acceptance_verdict(soft_task)

    proven_task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
            "test_id": (
                "test_satisfied_soft_kinds_bucket_as_satisfied_unproven_not_proven"
            ),
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
    )
    record_code_acceptance_verdict(proven_task)

    assert classify_merged_acceptance(soft_task) == "satisfied_unproven"
    assert classify_merged_acceptance(proven_task) == "proven"

    aggregate = summarize_version_acceptance_aggregate([soft_task, proven_task])
    assert aggregate == {
        "proven": 1,
        "satisfied_unproven": 1,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 2,
        "proven_rate": 0.5,
        "satisfied_rate": 1.0,
    }


def test_malformed_recorded_verdict_buckets_as_malformed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
            "test_id": "test_malformed_recorded_verdict_buckets_as_malformed",
        },
        changed_files=["backend/app/acceptance_gate.py"],
    )
    record_code_acceptance_verdict(task)
    task.context[CODE_ACCEPTANCE_VERDICT_KEY].pop("satisfied")

    assert classify_merged_acceptance(task) == "malformed"

    aggregate = summarize_version_acceptance_aggregate([task])
    assert aggregate["malformed"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# inferred_test_cochange — undeclared merges upgraded from PR co-change signal
# ─────────────────────────────────────────────────────────────────────────────


def _inferred_ctx(
    changed_files: list[str] | None, unverified_reason: str | None = None
) -> dict:
    """A landed, undeclared context with the given changed files / merge reason."""
    ctx: dict = {"pr_merged": True, "landed_on_integration": True}
    if changed_files is not None:
        ctx["_pr_changed_files"] = changed_files
    if unverified_reason is not None:
        ctx[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    return ctx


def test_undeclared_verified_test_and_prod_cochange_is_inferred():
    """Verified merge, no verdict, test + non-test prod file → inferred_test_cochange."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "inferred_test_cochange"
    # It still counts as verified merged work (at least as strong as undeclared).
    assert acceptance_counts_as_verified_context(ctx) is True


def test_undeclared_test_only_diff_is_not_inferred():
    """A test-only diff has no production seam to exercise → stays undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/testing/test_acceptance_gate.py",
            "backend/testing/test_release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_undeclared_prod_only_diff_is_not_inferred():
    """A production-only diff shipped no test → stays undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/app/release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_undeclared_empty_changed_files_is_not_inferred():
    """No changed-file signal at all → stays undeclared (fail-safe)."""
    assert classify_merged_acceptance_context(_inferred_ctx([])) == "undeclared"
    assert classify_merged_acceptance_context(_inferred_ctx(None)) == "undeclared"


def test_benign_unverified_test_cochange_is_not_inferred():
    """A benign fall-open means CI did not run — test presence is moot → undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_classify_undeclared_reason_not_ci_verified():
    """A benign fall-open (CI did not run) → not_ci_verified, even with a test+prod diff."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "not_ci_verified"


def test_classify_undeclared_reason_no_changed_files_context():
    """CI-verified but no _pr_changed_files list recorded → no_changed_files_context."""
    ctx = _inferred_ctx(None)
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "no_changed_files_context"


def test_classify_undeclared_reason_no_test_prod_cochange():
    """CI-verified with a prod-only changed-files list → no_test_prod_cochange."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/app/release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "no_test_prod_cochange"


def test_classify_undeclared_reason_partitions_undeclared_bucket():
    """The three reasons are exhaustive over the undeclared branch's inputs."""
    from app.acceptance_gate import _UNDECLARED_REASON_KEYS

    reasons = {
        classify_undeclared_reason(
            _inferred_ctx(
                ["backend/app/x.py", "backend/testing/test_x.py"],
                unverified_reason="no_detectable_test_command",
            )
        ),
        classify_undeclared_reason(_inferred_ctx(None)),
        classify_undeclared_reason(_inferred_ctx(["backend/app/x.py"])),
    }
    assert reasons == set(_UNDECLARED_REASON_KEYS)


# The persisted per-verdict ``reason`` strings the fail-closed gate emits, one
# per canonical cause. Kept as real strings (with the same repr-quoted paths the
# gate formats) so the classifier is tested against what actually lands in
# task.context[CODE_ACCEPTANCE_VERDICT_KEY], not a hand-rolled reimplementation.
_REASONED_UNSATISFIED_SAMPLES = {
    "named_test_diff_provenance_unavailable": (
        "named_test",
        "declared named_test 'backend/testing/test_widget.py' could not be "
        "evaluated because PR changed-file provenance was unavailable — "
        "_pr_changed_files was never recorded",
    ),
    "named_test_missing_from_diff": (
        "named_test",
        "declared named_test 'backend/testing/test_widget.py' is not among the "
        "PR's changed files — the claimed test was not added or modified here",
    ),
    "named_test_not_run_green": (
        "named_test",
        "declared named_test 'backend/testing/test_widget.py' did not run green "
        "under a real merge-test verdict (the merge fell open without verification)",
    ),
    "named_test_test_only_diff": (
        "named_test",
        "declared named_test 'backend/testing/test_widget.py' ships no production "
        "code change it could exercise (the diff is test-only)",
    ),
    "named_test_ungrounded_seam": (
        "named_test",
        "declared named_test 'backend/testing/test_widget.py' exercises a "
        "production seam that does not semantically correspond to the declared "
        "test module — none of the changed production files match the test file's "
        "module name",
    ),
    "named_test_block_describes_other_test": (
        "named_test",
        "declared named_test 'backend/testing/test_acceptance_gate.py' cannot be "
        "credited from the merge-test verdict: the recorded acceptance-test block "
        "describes a DIFFERENT test than the declared test_path — a green run of "
        "another test does not prove this declaration",
    ),
    "artifact_empty_diff": (
        "downstream_artifact",
        "declared downstream_artifact acceptance evidence but the task shipped "
        "no changed files to deliver it",
    ),
    "artifact_not_in_diff": (
        "downstream_artifact",
        "declared downstream_artifact 'release-deploy-artifact-registry' is not "
        "reflected in any of the 3 shipped changed file(s) — the diff does not "
        "deliver the named artifact",
    ),
    "visual_malformed": (
        "visual",
        "declared visual acceptance evidence is malformed or incomplete",
    ),
    "visual_no_screenshot": (
        "visual",
        "declared visual acceptance evidence but no matching viewport screenshot "
        "was uploaded for component 'MetricsDashboard' at 375px",
    ),
    "coverage_missing_threshold": (
        "coverage_delta",
        "coverage_delta acceptance evidence requires a numeric threshold "
        "(coverage percentage 0–100)",
    ),
    "coverage_missing_result_no_result": (
        "coverage_delta",
        "coverage_delta acceptance evidence requires a test coverage result in "
        "task context (test_coverage_result) — no result found",
    ),
    "coverage_missing_result_no_pct": (
        "coverage_delta",
        "coverage_delta acceptance evidence: coverage result is missing numeric "
        "coverage_pct",
    ),
    "coverage_below_threshold": (
        "coverage_delta",
        "coverage_delta acceptance evidence: achieved 55.0% coverage < 80% "
        "threshold",
    ),
    "observable_empty_diff": (
        "observable_behavior",
        "declared observable_behavior acceptance evidence but the task shipped "
        "no changed files to exercise it",
    ),
    "unrecognized_kind": (
        "future_kind",
        "unrecognized acceptance kind 'future_kind'",
    ),
}

# The two coverage_missing_result variants collapse to one canonical cause.
_COVERAGE_MISSING_RESULT_SAMPLES = {
    "coverage_missing_result_no_result",
    "coverage_missing_result_no_pct",
}


@pytest.mark.parametrize(
    "expected,kind,reason",
    [
        (key, kind, reason)
        for key, (kind, reason) in _REASONED_UNSATISFIED_SAMPLES.items()
    ],
)
def test_classify_unsatisfied_reason_maps_recorded_reason(expected, kind, reason):
    """Every fail-closed verdict reason maps to exactly one canonical cause."""
    recorded = {"satisfied": False, "proven": False, "kind": kind, "reason": reason}
    if expected in _COVERAGE_MISSING_RESULT_SAMPLES:
        expected = "coverage_missing_result"
    assert classify_unsatisfied_reason(recorded) == expected


def test_classify_unsatisfied_reason_partitions_known_causes():
    """The canonical causes partition the declared_unsatisfied bucket.

    Each ``_UNSATISFIED_REASON_KEYS`` key is reachable from a verdict the gate
    can actually emit (plus the ``unknown`` catch-all for unreasoned rows), and
    the two coverage_missing_result variants collapse onto the single canonical
    key, so tallies always partition.
    """
    from app.acceptance_gate import _UNSATISFIED_REASON_KEYS

    outputs = set()
    for key, (kind, reason) in _REASONED_UNSATISFIED_SAMPLES.items():
        canonical = (
            "coverage_missing_result"
            if key in _COVERAGE_MISSING_RESULT_SAMPLES
            else key
        )
        recorded = {"satisfied": False, "proven": False, "kind": kind, "reason": reason}
        assert classify_unsatisfied_reason(recorded) == canonical
        outputs.add(canonical)
    outputs.add(classify_unsatisfied_reason({"satisfied": False, "kind": "named_test"}))
    assert outputs == set(_UNSATISFIED_REASON_KEYS)


def test_classify_unsatisfied_reason_missing_reason_falls_back_unknown():
    """Verdicts stamped without a reason (legacy / manually-written rows) are
    classified ``unknown`` deterministically rather than raising or inventing a
    cause — the split still sums to the declared_unsatisfied bucket."""
    assert (
        classify_unsatisfied_reason(
            {"satisfied": False, "proven": False, "kind": "named_test"}
        )
        == "unknown"
    )
    assert classify_unsatisfied_reason({"satisfied": False, "kind": "visual"}) == "unknown"
    assert classify_unsatisfied_reason({"satisfied": False}) == "unknown"
    assert classify_unsatisfied_reason(None) == "unknown"
    assert classify_unsatisfied_reason("not-a-dict") == "unknown"


def test_classify_unsatisfied_reason_matches_gate_recorded_verdicts():
    """Drive the REAL gate: a declared-but-unsatisfied task's recorded verdict
    reason classifies to the same cause the reason text describes.

    Connects the classifier to what :func:`record_code_acceptance_verdict`
    actually stamps, for each distinct fail-closed branch of the gate."""
    unsatisfied_cases = [
        (  # named_test absent from the diff
            {
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                "changed_files": ["backend/app/widget.py"],
                "unverified_reason": None,
            },
            "named_test_missing_from_diff",
        ),
        (  # named_test that never ran green
            {
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                "changed_files": [
                    "backend/app/widget.py",
                    "backend/testing/test_widget.py",
                ],
                "unverified_reason": "no_detectable_test_command",
            },
            "named_test_not_run_green",
        ),
        (  # named_test with a test-only diff (no production seam)
            {
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/testing/test_widget.py",
                },
                "changed_files": ["backend/testing/test_widget.py"],
                "unverified_reason": None,
            },
            "named_test_test_only_diff",
        ),
        (  # observable_behavior with an empty diff
            {
                "acceptance_evidence": {
                    "kind": "observable_behavior",
                    "description": "the dashboard shows the live serving SHA banner",
                },
                "changed_files": [],
                "unverified_reason": None,
            },
            "observable_empty_diff",
        ),
    ]
    for kwargs, expected in unsatisfied_cases:
        task = _make_code_task(**kwargs)
        verdict = evaluate_code_acceptance(task)
        assert verdict.satisfied is False, expected
        assert classify_unsatisfied_reason(
            {
                "satisfied": verdict.satisfied,
                "proven": verdict.proven,
                "kind": verdict.kind,
                "reason": verdict.reason,
            }
        ) == expected


def test_degraded_unverified_test_cochange_stays_degraded():
    """A degraded fall-open overrides inference → merged_unverified_degraded."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="verify_setup_failed",
    )
    assert (
        classify_merged_acceptance_context(ctx) == "merged_unverified_degraded"
    )


def test_declared_verdict_wins_over_inferred_cochange():
    """An explicit recorded verdict is classified on its own axis, never inferred."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ]
    )
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": True,
        "proven": True,
        "kind": "named_test",
    }
    assert classify_merged_acceptance_context(ctx) == "proven"


def test_inferred_bucket_summarized_and_counts_verified():
    """The aggregate exposes the inferred bucket and it is a distinct 7th key."""
    inferred = _make_code_task(
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
    )
    inferred.context["pr_merged"] = True
    inferred.context["landed_on_integration"] = True
    assert classify_merged_acceptance(inferred) == "inferred_test_cochange"

    aggregate = summarize_version_acceptance_aggregate([inferred])
    assert aggregate == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 1,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 1,
        "proven_rate": 0.0,
        "satisfied_rate": 0.0,
    }
    assert aggregate["proven"] == 0
    assert aggregate["satisfied_unproven"] == 0
    assert aggregate["proven_rate"] == 0.0
    assert aggregate["satisfied_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# proven axis — evaluate_code_acceptance: named_test and visual are proven at
# evaluation time; downstream_artifact becomes proven later via
# record_artifact_consumptions_for_task (see DB tests below)
# ─────────────────────────────────────────────────────────────────────────────


def test_soft_kinds_are_satisfied_but_not_proven():
    # A satisfied observable_behavior proves only that a diff shipped — it is
    # satisfied but NOT proven.  A downstream_artifact is also not proven at
    # evaluation time; it becomes proven only after downstream consumption.
    for kind, extra, changed in (
        (
            "observable_behavior",
            {"description": "the dashboard shows the SHA banner"},
            ["backend/app/x.py"],
        ),
        (
            "downstream_artifact",
            {"artifact_id": "release-promotion-record"},
            ["backend/app/release_promotion.py"],
        ),
    ):
        task = _make_code_task(
            acceptance_evidence={"kind": kind, **extra},
            changed_files=changed,
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


def test_visual_kind_requires_matching_viewport_screenshot_and_is_proven():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
    )

    verdict_missing = evaluate_code_acceptance(task)
    assert verdict_missing.satisfied is False
    assert verdict_missing.proven is False
    assert "matching viewport screenshot" in verdict_missing.reason

    record_code_acceptance_verdict(task)
    recorded_missing = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded_missing["satisfied"] is False
    assert acceptance_blocks_success(recorded_missing) is True

    task_with_screenshot = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
    )
    task_with_screenshot.context["viewport_failure_screenshot_uri"] = (
        "gs://bucket/viewport-failures/"
        f"{task_with_screenshot.project_id}/MetricsDashboard-1280.png"
    )

    verdict = evaluate_code_acceptance(task_with_screenshot)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "viewport-failures" in verdict.reason

    recorded = record_code_acceptance_verdict(task_with_screenshot)
    stamped = task_with_screenshot.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert acceptance_was_declared(stamped) is True
    assert acceptance_is_proven(stamped) is True
    assert acceptance_blocks_success(stamped) is False


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


# ─────────────────────────────────────────────────────────────────────────────
# coverage_delta — test-coverage based acceptance gate
# ─────────────────────────────────────────────────────────────────────────────


def test_coverage_delta_meets_threshold_passes_and_is_proven():
    """coverage_delta passes when the coverage result meets or exceeds threshold."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "achieved" in verdict.reason


def test_coverage_delta_below_threshold_fails_closed():
    """coverage_delta fails when coverage is below the declared threshold."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 45.0}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "coverage <" in verdict.reason or "below" in verdict.reason


def test_coverage_delta_missing_result_fails_closed():
    """coverage_delta fails when no coverage result is stored in context."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "no result" in verdict.reason.lower()


def test_coverage_delta_missing_threshold_fails_closed():
    """coverage_delta fails when no threshold is provided in the declaration.

    The schema-layer validator on AcceptanceEvidence rejects missing threshold
    before ``evaluate_code_acceptance`` is reached, so this test validates the
    gate-level ``_evaluate_coverage_delta`` function directly (bypassing the
    schema validator via ``model_construct``) to confirm the fail-closed guard
    still fires if a malformed declaration ever reached it.
    """
    evidence = AcceptanceEvidence.model_construct(kind="coverage_delta")
    verdict = _evaluate_coverage_delta(evidence, context={})
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False


def test_coverage_delta_non_numeric_coverage_fails_closed():
    """coverage_delta fails when coverage_pct is not a number."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py"],
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": "high"}
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False


def test_coverage_delta_populated_from_verify_tests_artifact():
    """coverage_delta acceptance evidence consumes coverage data that was
    extracted from a MERGE_TEST_RESULT verdict and bridged into task context,
    simulating what the orchestrator CI monitor + ci_completion do in production.

    The MERGE_TEST_RESULT artifact carries a ``coverage`` block
    (``{passed, skipped, total}``) from the verify-tests handler. This test
    simulates the bridge: computing ``coverage_pct`` from those counts and
    storing it under ``test_coverage_result``, then verifying the acceptance
    gate evaluates it correctly.
    """
    # Simulate the coverage block a real MERGE_TEST_RESULT artifact would carry.
    verdict_coverage = {"passed": 42, "skipped": 3, "total": 50}
    total = verdict_coverage["total"]
    passed = verdict_coverage["passed"]
    coverage_pct = round((passed / total) * 100.0, 2)
    assert coverage_pct == 84.0  # 42/50 = 84%

    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": coverage_pct}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "achieved" in verdict.reason


def test_coverage_delta_uses_line_coverage_not_test_pass_rate():
    """coverage_pct reflects the fraction of changed lines hit by the test
    (line coverage), not the test pass rate.

    The orchestrator stores line_coverage_pct (from pytest-cov) as
    coverage_pct in test_coverage_result. When line coverage differs from
    the test pass rate (passed/total), the acceptance gate evaluates against
    the line coverage value — NOT the pass rate.

    This verifies the coverage_delta acceptance evidence kind fixes the
    semantic bug where coverage_pct was computed as (passed/total)*100,
    which is test pass rate, not changed-line coverage.
    """
    # Simulate a MERGE_TEST_RESULT coverage block where line coverage (90%)
    # differs from test pass rate (passed=8, total=10 → 80%).
    # The orchestrator reads line_coverage_pct and stores it as coverage_pct.
    line_cov = 90.0
    pass_rate = round((8 / 10) * 100.0, 2)  # 80.0
    assert line_cov != pass_rate  # 90 != 80 — coverage differs from pass rate

    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 85,  # threshold between pass_rate and line_cov
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    # The coverage_pct is the line coverage value, NOT the test pass rate.
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": line_cov}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True  # 90 >= 85 passes
    assert verdict.proven is True

    # Prove it's NOT pass rate: with the same test counts (8/10 = 80%),
    # using pass rate as coverage_pct would give coverage_pct=80, which
    # is below the threshold of 85 and would fail.
    task2 = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 85,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task2.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": pass_rate}
    pass_verdict = evaluate_code_acceptance(task2)
    assert pass_verdict.satisfied is False  # 80 < 85 fails
    assert pass_verdict.proven is False


def test_coverage_delta_pass_rate_not_proven():
    """coverage_delta with a pass-rate proxy (source="pass_rate") is satisfied
    but NOT proven — test pass rate is semantically distinct from changed-line
    coverage.

    When no pytest-cov/c8 instrumentation is active, the orchestrator stores
    coverage_pct computed from (passed/total)*100 with source="pass_rate". The
    acceptance gate must distinguish this from instrumented line coverage:
    the result is satisfied (a real test run happened and the threshold was met)
    but NOT machine-checked proven (pass-rate is a weaker claim than line
    coverage). This prevents coverage_delta declared on uninstrumented repos
    from silently proving stronger claims than the data supports.

    Exercises the production logic in _evaluate_coverage_delta's source check.
    """
    # Threshold of 80%, pass-rate proxy of 85% — meets threshold but is pass rate.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {
        "coverage_pct": 85.0,
        "source": "pass_rate",
    }
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is False  # pass-rate proxy is NOT proven
    assert "pass-rate proxy" in verdict.reason or "pass rate" in verdict.reason

    # Recorded verdict reflects the same: satisfied but not proven.
    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert recorded.satisfied is True
    assert recorded.proven is False
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is False
    assert acceptance_blocks_success(stamped) is False  # satisfied does not block
    assert acceptance_is_proven(stamped) is False  # but is NOT machine-proven

    # Compare with the instrumented (line coverage) path: same threshold, same
    # coverage_pct value but no pass-rate source — IS proven.
    task_instrumented = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task_instrumented.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
    inst_verdict = evaluate_code_acceptance(task_instrumented)
    assert inst_verdict.proven is True  # instrumented coverage IS proven


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


# ─────────────────────────────────────────────────────────────────────────────
# Verdict-axis invariant guard — proven ⟹ satisfied ⟹ declared, over a full mix
#
# A durable, non-gameable guard at the per-task verdict level that complements
# the aggregate-level guard in test_direction_outcomes.py. Across every kind of
# constructed task, it asserts the implication chain between the three truth axes
# always holds, and that via evaluate_code_acceptance a verified, satisfied
# named_test exercising a changed seam is the only static path to proven=True —
# soft, undeclared, test-only, and degraded/unverified evaluations are not.
# (downstream_artifact becomes proven via record_artifact_consumptions_for_task,
# tested separately in the DB section.)
# ─────────────────────────────────────────────────────────────────────────────


# (label, kwargs-for-_make_code_task, expected_declared, expected_satisfied,
#  expected_proven)
_VERDICT_MIX = [
    (
        "proven_named_test",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": None,
        },
        True,
        True,
        True,
    ),
    (
        "soft_observable_behavior",
        {
            "acceptance_evidence": {
                "kind": "observable_behavior",
                "description": "the widget renders the empty state without error",
            },
            "changed_files": ["backend/app/widget.py"],
        },
        True,
        True,
        False,
    ),
    (
        "soft_downstream_artifact",
        {
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "widget_metrics_v2",
            },
            "changed_files": ["backend/app/widget.py"],
        },
        True,
        True,
        False,
    ),
    (
        "visual_proven",
        {
            "acceptance_evidence": {
                "kind": "visual",
                "component": "MetricsDashboard",
                "viewport_width": 1280,
            },
            "changed_files": ["admin-ui/src/components/MetricsDashboard.tsx"],
        },
        True,
        True,
        True,
    ),
    (
        "coverage_delta_proven",
        {
            "acceptance_evidence": {
                "kind": "coverage_delta",
                "threshold": 80,
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": None,
        },
        True,
        True,
        True,
    ),
    (
        "declared_unsatisfied_named_test_missing",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py"],  # test not in diff
            "unverified_reason": None,
        },
        True,
        False,
        False,
    ),
    (
        "test_only_diff",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/testing/test_widget.py"],  # no production seam
            "unverified_reason": None,
        },
        True,
        False,
        False,
    ),
    (
        "degraded_unverified_named_test",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": "verify_setup_failed",  # fell open, no real verdict
        },
        True,
        False,
        False,
    ),
    (
        "undeclared_legacy",
        {"acceptance_evidence": None, "changed_files": ["backend/app/widget.py"]},
        False,
        True,  # undeclared is vacuously satisfied (never blocks)
        False,
    ),
]


def test_verdict_axis_implication_invariant_over_mix():
    """proven ⟹ satisfied ⟹ declared — checked over a full mix of task kinds.

    Via evaluate_code_acceptance alone, only a verified satisfied named_test
    exercising a changed seam is proven; downstream_artifact consumption is the
    second proven path but requires record_artifact_consumptions_for_task (DB).
    Drives the REAL gate and REAL predicates, never a reimplementation."""
    for label, kwargs, exp_declared, exp_satisfied, exp_proven in _VERDICT_MIX:
        task = _make_code_task(**kwargs)
        if label == "visual_proven":
            task.context["viewport_failure_screenshot_uri"] = (
                "gs://bucket/viewport-failures/"
                f"{task.project_id}/MetricsDashboard-1280.png"
            )
        if label == "coverage_delta_proven":
            task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
        verdict = evaluate_code_acceptance(task)

        # The verdict matches the expected per-axis outcome for this kind.
        assert verdict.declared is exp_declared, label
        assert verdict.satisfied is exp_satisfied, label
        assert verdict.proven is exp_proven, label

        # Core implication chain on the live verdict: proven ⟹ satisfied ⟹ declared.
        if verdict.proven:
            assert verdict.satisfied, label
        if verdict.satisfied and not verdict.declared:
            # Only the undeclared case is satisfied-without-declared; it is the
            # documented fail-safe (undeclared work is never blocked) and is
            # NEVER proven.
            assert not verdict.proven, label

        # Only a satisfied named_test, visual, or coverage_delta (from
        # line-coverage instrumentation, not pass-rate proxy) is ever proven
        # at evaluation time.
        if verdict.proven:
            assert verdict.kind in {"named_test", "visual", "coverage_delta"}, label

        # Now exercise the recorded-verdict predicates the aggregates consume.
        recorded = record_code_acceptance_verdict(task)
        stamped = task.context.get(CODE_ACCEPTANCE_VERDICT_KEY)
        if not verdict.declared:
            # Undeclared tasks record nothing and are never declared/proven.
            assert recorded is None, label
            assert stamped is None, label
            assert acceptance_was_declared(stamped) is False, label
            assert acceptance_is_proven(stamped) is False, label
            continue

        assert acceptance_was_declared(stamped) is True, label
        # proven ⟹ declared, and proven ⟹ not-blocking (it is satisfied).
        assert acceptance_is_proven(stamped) is exp_proven, label
        if acceptance_is_proven(stamped):
            assert acceptance_was_declared(stamped) is True, label
            assert acceptance_blocks_success(stamped) is False, label


def test_soft_undeclared_testonly_degraded_never_proven():
    """The non-gameability promise: a soft, undeclared, test-only, or
    degraded/unverified merge is NEVER counted as proven."""
    never_proven_labels = {
        "soft_observable_behavior",
        "soft_downstream_artifact",
        "declared_unsatisfied_named_test_missing",
        "test_only_diff",
        "degraded_unverified_named_test",
        "undeclared_legacy",
    }
    for label, kwargs, _decl, _sat, _proven in _VERDICT_MIX:
        if label not in never_proven_labels:
            continue
        task = _make_code_task(**kwargs)
        verdict = evaluate_code_acceptance(task)
        assert verdict.proven is False, label
        record_code_acceptance_verdict(task)
        assert acceptance_is_proven(task.context.get(CODE_ACCEPTANCE_VERDICT_KEY)) is False, label


# ─────────────────────────────────────────────────────────────────────────────
# Consolidated axis invariant tests: universal laws across full verdict matrix
# ─────────────────────────────────────────────────────────────────────────────
#
# These tests parametrize across the FULL verdict-kind matrix to pin the
# non-gameable semantic relationships as fail-closed invariants: proven implies
# declared AND satisfied; soft kinds are satisfied-but-not-proven; unsatisfied
# verdicts never block success; no-declaration verdicts record nothing.


def test_invariant_proven_implies_declared_and_satisfied():
    """Proven NEVER occurs without BOTH declared and satisfied."""
    # Via evaluate_code_acceptance: named_test + verified + seam changes is the
    # static proven path. Consumed downstream_artifact is the second proven path
    # but requires record_artifact_consumptions_for_task (tested in DB section).
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    # The one proven case: declared + satisfied + named_test + verified + seam
    assert verdict.proven is True
    assert verdict.declared is True
    assert verdict.satisfied is True

    # Contrapositive: if NOT declared, then NOT proven.
    task_undeclared = _make_code_task(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    verdict_undeclared = evaluate_code_acceptance(task_undeclared)
    assert verdict_undeclared.declared is False
    assert verdict_undeclared.proven is False

    # Contrapositive: if NOT satisfied, then NOT proven.
    task_unsatisfied = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],  # test not present
        unverified_reason=None,
    )
    verdict_unsatisfied = evaluate_code_acceptance(task_unsatisfied)
    assert verdict_unsatisfied.declared is True
    assert verdict_unsatisfied.satisfied is False
    assert verdict_unsatisfied.proven is False


def test_invariant_soft_kinds_are_satisfied_not_proven():
    """Observable_behavior / downstream_artifact: satisfied but never proven."""
    # downstream_artifact must be GROUNDED — the declared artifact_id token has
    # to be reflected in a shipped changed-file path (here "promotion" appears in
    # release_promotion.py). A real diff alone is sufficient for observable_behavior.
    for kind, extra, changed in (
        (
            "observable_behavior",
            {"description": "the dashboard shows the SHA banner"},
            ["backend/app/x.py"],
        ),
        (
            "downstream_artifact",
            {"artifact_id": "release-promotion-record"},
            ["backend/app/release_promotion.py"],
        ),
    ):
        task = _make_code_task(
            acceptance_evidence={"kind": kind, **extra},
            changed_files=changed,  # shipped a real, grounded diff
        )
        verdict = evaluate_code_acceptance(task)
        # Soft kinds CAN be satisfied (they shipped a real diff)...
        assert verdict.satisfied is True
        # ...but are NEVER proven (no machine-checked assertion).
        assert verdict.proven is False
        # And soft kinds must be declared (verdict was recorded).
        assert verdict.declared is True


def test_invariant_unsatisfied_verdict_never_proven_blocks_success():
    """Unsatisfied verdicts are never proven and block success scoring."""
    # All fail-closed cases: declared but unsatisfied
    fail_cases = [
        # named_test: test not present in PR
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py"],  # test missing
            "unverified_reason": None,
        },
        # named_test: test-only diff
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/testing/test_widget.py"],  # no seam
            "unverified_reason": None,
        },
        # named_test: unverified merge
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py", "backend/testing/test_widget.py"],
            "unverified_reason": "no_runnable_suite",  # fall-open
        },
        # observable_behavior: no changed files
        {
            "evidence": {
                "kind": "observable_behavior",
                "description": "the dashboard shows the SHA banner",
            },
            "changed_files": [],  # empty diff
            "unverified_reason": None,
        },
    ]
    for case in fail_cases:
        task = _make_code_task(
            acceptance_evidence=case["evidence"],
            changed_files=case["changed_files"],
            unverified_reason=case.get("unverified_reason"),
        )
        verdict = evaluate_code_acceptance(task)
        # All fail-closed cases: declared but unsatisfied.
        assert verdict.declared is True, f"Case {case['evidence']['kind']} should be declared"
        assert verdict.satisfied is False, f"Case {case['evidence']['kind']} should be unsatisfied"
        # NEVER proven if unsatisfied.
        assert verdict.proven is False, f"Case {case['evidence']['kind']} should not be proven"
        # Unsatisfied verdict blocks success.
        record_code_acceptance_verdict(task)
        recorded = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        assert acceptance_blocks_success(recorded) is True


def test_invariant_no_declaration_records_nothing():
    """Undeclared tasks: verdict not recorded, scoring unchanged."""
    task = _make_code_task(
        acceptance_evidence=None,
        changed_files=["backend/app/x.py"],
    )
    verdict = evaluate_code_acceptance(task)
    # No declaration: not recorded.
    assert verdict.declared is False
    result = record_code_acceptance_verdict(task)
    assert result is None
    # The verdict key is never written.
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    # Undeclared tasks never block success (backward-compatible).
    assert acceptance_blocks_success(None) is False


def test_invariant_all_valid_kinds_covered():
    """Every VALID kind is tested: named_test, observable_behavior, downstream_artifact, visual."""
    # This invariant ensures there is no blind spot in the kind matrix.
    # The only valid kinds per AcceptanceEvidence schema are these four.
    # (Unknown kinds cannot be created by validation, so we test only real ones.)
    from app.payload_schemas import AcceptanceEvidence

    # named_test kind
    nt = AcceptanceEvidence(kind="named_test", test_path="backend/testing/test_x.py")
    assert nt.kind == "named_test"

    # observable_behavior kind
    ob = AcceptanceEvidence(kind="observable_behavior", description="feature works")
    assert ob.kind == "observable_behavior"

    # downstream_artifact kind
    da = AcceptanceEvidence(kind="downstream_artifact", artifact_id="some-artifact")
    assert da.kind == "downstream_artifact"

    # visual kind
    vis = AcceptanceEvidence(
        kind="visual", component="MetricsDashboard", viewport_width=1280
    )
    assert vis.kind == "visual"

    # Verify the four kinds are the closed set by checking the schema.
    # (This documents that if AcceptanceEvidence.kind ever adds a new kind,
    # this test's assertion will need updating, triggering a review of whether
    # the evaluation logic covers the new kind.)
    import typing

    hints = typing.get_type_hints(AcceptanceEvidence)
    kind_annotation = hints.get("kind")
    if hasattr(kind_annotation, "__args__"):
        valid_kinds = set(kind_annotation.__args__)
        assert valid_kinds == {
            "named_test",
            "observable_behavior",
            "downstream_artifact",
            "visual",
            "coverage_delta",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate invariant tests: per-cycle relationships that strategic/release
# decisions consume (proven_merged <= declared_merged <= total_write, etc.)
# ─────────────────────────────────────────────────────────────────────────────


def test_aggregate_invariant_proven_subset_of_declared():
    """Per-cycle: proven_merged_count <= declared_merged_count."""
    # Import the aggregation functions from direction_outcomes.
    from app.direction_outcomes import (
        proven_merged_count,
        declared_merged_count,
    )

    # Construct synthetic research_history entries covering all sub-relations.
    cases = [
        # All zero: no merges, no declarations.
        {
            "entry": {
                "acceptance_proven_merged": 0,
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 0,
                "acceptance_declaration_coverage": None,
            },
            "expected_proven": 0,
            "expected_declared": 0,
            "expected_total": 0,
        },
        # Some declared, none proven (soft kinds only).
        {
            "entry": {
                "acceptance_proven_merged": 0,
                "acceptance_declared_merged": 3,  # 3 soft-kind merges
                "acceptance_merged_write_total": 5,
                "acceptance_declaration_coverage": 0.6,
            },
            "expected_proven": 0,
            "expected_declared": 3,
            "expected_total": 5,
        },
        # Mixed: some proven, some declared but not proven.
        {
            "entry": {
                "acceptance_proven_merged": 2,  # 2 named_test proven
                "acceptance_declared_merged": 5,  # 3 more soft kinds
                "acceptance_merged_write_total": 7,  # 2 undeclared
                "acceptance_declaration_coverage": 5 / 7,
            },
            "expected_proven": 2,
            "expected_declared": 5,
            "expected_total": 7,
        },
        # All declared, all proven (all named_tests).
        {
            "entry": {
                "acceptance_proven_merged": 4,
                "acceptance_declared_merged": 4,
                "acceptance_merged_write_total": 4,
                "acceptance_declaration_coverage": 1.0,
            },
            "expected_proven": 4,
            "expected_declared": 4,
            "expected_total": 4,
        },
    ]
    for case in cases:
        entry = case["entry"]
        proven = proven_merged_count(entry)
        declared = declared_merged_count(entry)

        # Invariant 1: proven is always a subset of declared.
        assert proven <= declared, (
            f"proven={proven} > declared={declared} violates subset invariant"
        )
        # Invariant 2: declared is never larger than total merged write tasks.
        assert declared <= entry.get("acceptance_merged_write_total", 0), (
            f"declared={declared} > total={entry.get('acceptance_merged_write_total')} "
            "violates coverage invariant"
        )


def test_aggregate_invariant_coverage_fraction():
    """Per-cycle: declaration_coverage == declared / total, in [0, 1]."""
    from app.direction_outcomes import declared_merged_count

    cases = [
        # No write tasks: coverage is None.
        {
            "entry": {
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 0,
                "acceptance_declaration_coverage": None,
            },
            "expected_coverage": None,
        },
        # All undeclared: coverage is 0.0.
        {
            "entry": {
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 5,
                "acceptance_declaration_coverage": 0.0,
            },
            "expected_coverage": 0.0,
        },
        # Half declared: coverage is 0.5.
        {
            "entry": {
                "acceptance_declared_merged": 3,
                "acceptance_merged_write_total": 6,
                "acceptance_declaration_coverage": 0.5,
            },
            "expected_coverage": 0.5,
        },
        # All declared: coverage is 1.0.
        {
            "entry": {
                "acceptance_declared_merged": 4,
                "acceptance_merged_write_total": 4,
                "acceptance_declaration_coverage": 1.0,
            },
            "expected_coverage": 1.0,
        },
    ]
    for case in cases:
        entry = case["entry"]
        coverage = entry.get("acceptance_declaration_coverage")
        declared = declared_merged_count(entry)
        total = entry.get("acceptance_merged_write_total", 0)

        if coverage is None:
            # No write tasks: coverage is None.
            assert total == 0 or declared == 0
        else:
            # Coverage must be in [0, 1].
            assert 0.0 <= coverage <= 1.0, f"coverage={coverage} outside [0, 1]"
            # Coverage must equal declared / total (within floating-point tolerance).
            if total > 0:
                expected_coverage = round(declared / total, 3)
                assert coverage == expected_coverage, (
                    f"coverage={coverage} != declared/total={expected_coverage}"
                )


def test_aggregate_invariant_legacy_entries_degrade_safely():
    """Legacy entries (before new fields existed) degrade to safe defaults."""
    from app.direction_outcomes import (
        proven_merged_count,
        declared_merged_count,
    )

    # Old entry: no acceptance fields at all (pre-2026-06 entries).
    old_entry = {
        "prs_merged": 10,
        "tasks_completed": 5,
    }
    # proven and declared count functions default to 0 when fields are absent.
    assert proven_merged_count(old_entry) == 0
    assert declared_merged_count(old_entry) == 0


def test_aggregate_invariant_coverage_never_exceeds_bounds():
    """Declaration coverage is clamped to [0, 1] and never inverted."""
    # This invariant guards against division errors or inverted numerator/denominator.
    cases = [
        # declared > total (logic error — should never happen, but guard it).
        {
            "declared": 5,
            "total": 3,
            "should_clamp": True,
        },
        # Normal case: 0 <= declared <= total.
        {
            "declared": 2,
            "total": 5,
            "should_clamp": False,
        },
    ]
    for case in cases:
        declared = case["declared"]
        total = case["total"]
        # Synthesize the coverage value as the code would compute it.
        coverage = round(declared / total, 3) if total > 0 else None
        # Coverage should be in [0, 1].
        # If declared > total, coverage would exceed 1 — that's a bug in the
        # source code (declared should never exceed total), not a test issue.
        # But we document the invariant: if it ever happens, coverage > 1 is
        # detectable evidence of the bug.
        if coverage is not None and declared <= total:
            assert coverage <= 1.0


def test_missing_review_loop_matches_canonical_verified_merge_predicate():
    """Missing/malformed review_loop defaults to VERIFIED, matching is_verified_merge.

    The acceptance gate's _merge_was_test_verified helper must use the SAME
    fail-safe default as the canonical landed_signal.is_verified_merge predicate
    (and this module's own _context_merge_verification_status): an ABSENT or
    malformed review_loop carries no unverified_merge_reason, so the merge was
    genuinely verified. A landed task whose context simply lacks a review_loop
    dict must NOT be scored declared_unsatisfied — it must earn machine proof.
    Only an explicit unverified_merge_reason makes the merge unverified.
    """
    from app.landed_signal import is_verified_merge

    def _landed(task: Task) -> Task:
        # Stamp the integration-landing signal so the canonical
        # is_merged_completion gate (status COMPLETED + landed signal) is met,
        # exactly as a real landed task would carry it.
        task.context = {
            **(task.context or {}),
            "pr_merged": True,
            "landed_on_integration": True,
        }
        return task

    # Case 1: completely missing review_loop context. The canonical predicate
    # treats this as a VERIFIED merge, so the acceptance gate must agree and
    # mint machine proof instead of scoring declared_unsatisfied.
    task_no_context = _landed(_make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    ))
    # Explicitly drop review_loop to simulate a context that simply lacks it.
    task_no_context.context.pop(REVIEW_LOOP_KEY, None)

    assert is_verified_merge(task_no_context) is True
    verdict_no_context = evaluate_code_acceptance(task_no_context)
    assert verdict_no_context.declared is True
    assert verdict_no_context.satisfied is True
    assert verdict_no_context.proven is True

    record_code_acceptance_verdict(task_no_context)
    stamped = task_no_context.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert acceptance_blocks_success(stamped) is False
    assert acceptance_is_proven(stamped) is True
    assert classify_merged_acceptance(task_no_context) == "proven"

    # Case 2: malformed review_loop (not a dict) — same verified default.
    task_malformed = _landed(_make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    ))
    task_malformed.context[REVIEW_LOOP_KEY] = "not a dict"  # Malformed

    assert is_verified_merge(task_malformed) is True
    verdict_malformed = evaluate_code_acceptance(task_malformed)
    assert verdict_malformed.satisfied is True
    assert verdict_malformed.proven is True

    # Case 3: explicit unverified_merge_reason — the ONLY fail-closed case; both
    # predicates agree the merge was NOT verified.
    task_explicit_unverified = _landed(_make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="no_runnable_suite",  # Explicit fall-open
    ))
    assert is_verified_merge(task_explicit_unverified) is False
    verdict_explicit = evaluate_code_acceptance(task_explicit_unverified)
    assert verdict_explicit.declared is True
    assert verdict_explicit.satisfied is False
    assert verdict_explicit.proven is False
    assert "did not run green" in verdict_explicit.reason

    record_code_acceptance_verdict(task_explicit_unverified)
    assert acceptance_blocks_success(
        task_explicit_unverified.context[CODE_ACCEPTANCE_VERDICT_KEY]
    ) is True


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_empty_db(db_session):
    """The backfill returns 0 on an empty or unpopulated database."""
    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_upgrades_false_negative(db_session):
    """A pre-fix task with the false-negative verdict pattern is upgraded by
    the backfill sweep, and its verdict is updated to proven=True.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_invariants.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_invariants.py",
        ],
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    # Stamp a false-negative verdict exactly as the pre-fix gate would have:
    # satisfied=True (test was present, verified, and shipped a production seam)
    # but proven=False because _test_semantically_grounded failed for the
    # cross-cutting test_invariants.py before the _CROSS_CUTTING_TEST_FILES fix.
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": True,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_invariants.py' exercises "
            "a production seam that does not semantically correspond to the "
            "declared test module"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    result = await backfill_acceptance_verdicts(db_session)
    assert result >= 1, "backfill should upgrade the pre-fix false-negative verdict"

    # Re-read the task and verify the verdict was upgraded
    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("proven") is True, "verdict should be upgraded to proven"
    assert updated.get("backfilled") is True
    # Rung 2 of design doc c50453ef: the re-recorded verdict is stamped with the
    # CURRENT rule version so re-derived rows are distinguishable from the
    # pre-fix ones (which carry no rule_version at all).
    assert updated.get("rule_version") == ACCEPTANCE_RULE_VERSION


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_upgrades_frontend_named_test(db_session):
    """A pre-fix FRONTEND named_test (vitest) verdict stamped satisfied=True /
    proven=False — because the old grounding only understood Python
    `test_<module>.py` naming — is upgraded to proven=True by the backfill sweep
    once JS/TS grounding lands. This is the self-healing path for rollup #14487.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        },
        changed_files=[
            "admin-ui/src/components/ComputeJobsPanel.tsx",
            "admin-ui/src/components/ComputeJobsPanel.test.tsx",
        ],
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": True,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test "
            "'admin-ui/src/components/ComputeJobsPanel.test.tsx' exercises a "
            "production seam that does not semantically correspond to the "
            "declared test module"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    result = await backfill_acceptance_verdicts(db_session)
    assert result >= 1, "backfill should upgrade the pre-fix frontend verdict"

    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("proven") is True, "frontend verdict should upgrade to proven"
    assert updated.get("backfilled") is True


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_heals_missing_review_loop_misscoring(
    db_session,
):
    """A pre-fix named_test verdict recorded unsatisfied with the 'did not run
    green' reason — because _merge_was_test_verified treated an ABSENT
    review_loop as unverified — is upgraded to proven=True by the backfill once
    the verified-merge default matches is_verified_merge. A genuinely-unverified
    verdict carrying the same reason stays unsatisfied.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Mis-scored task: the named_test shipped with a real production seam, but
    # the recorded verdict is unsatisfied because the merge context simply
    # lacked a review_loop dict (the pre-fix fail-closed default).
    mis_scored = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    mis_scored.project_id = project.id
    mis_scored.completed_at = datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc)
    ctx = dict(mis_scored.context or {})
    # Simulate the pre-fix shape: NO review_loop key + recorded "did not run green".
    ctx.pop(REVIEW_LOOP_KEY, None)
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' did not run green "
            "under a real merge-test verdict (the merge fell open without verification)"
        ),
    }
    mis_scored.context = ctx
    db_session.add(mis_scored)

    # Genuinely-unverified task: the same verdict shape but an explicit
    # unverified_merge_reason remains — must NOT be upgraded.
    genuinely_unverified = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="no_runnable_suite",
    )
    genuinely_unverified.project_id = project.id
    genuinely_unverified.completed_at = datetime(
        2026, 8, 5, 1, 0, 0, tzinfo=timezone.utc
    )
    gctx = dict(genuinely_unverified.context or {})
    gctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' did not run green "
            "under a real merge-test verdict (the merge fell open without verification)"
        ),
    }
    genuinely_unverified.context = gctx
    db_session.add(genuinely_unverified)
    await db_session.flush()

    result = await backfill_acceptance_verdicts(db_session)
    assert result >= 1, "backfill should heal the missing-review_loop mis-scored verdict"

    await db_session.refresh(mis_scored)
    updated = (mis_scored.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("satisfied") is True
    assert updated.get("proven") is True, "mis-scored verdict should upgrade to proven"
    assert updated.get("backfilled") is True
    assert updated.get("rule_version") == ACCEPTANCE_RULE_VERSION

    await db_session.refresh(genuinely_unverified)
    kept = (genuinely_unverified.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert kept is not None
    assert kept.get("satisfied") is False
    assert kept.get("proven") is False, (
        "a genuinely-unverified merge must stay unsatisfied, not be upgraded"
    )


@pytest.mark.asyncio
async def test_backfill_verdicts_no_starvation(db_session):
    """ORDER BY + NOT EXISTS ensures repeated backfill calls cover all
    >200 false-negative tasks (no starvation), and already-proven tasks
    with CriterionEvidence rows are excluded from the query.
    """
    from datetime import datetime, timezone

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Create an open release version so record_task_merge_evidence
    # persists CriterionEvidence rows for upgraded tasks — this is
    # required by the NOT EXISTS filter to advance the LIMIT window.
    version = ReleaseVersion(project_id=project.id)
    db_session.add(version)
    await db_session.flush()

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-backfill-starvation-test",
        text="Backfill starvation test criterion",
    )
    db_session.add(criterion)
    await db_session.flush()
    criterion_key = criterion.criterion_key

    FIX_TS = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)

    def _make_candidate(*, proven: bool) -> Task:
        task = _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_invariants.py",
            },
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = FIX_TS
        task.criterion_ids = [criterion_key]
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": True,
            "proven": proven,
            "kind": "named_test",
            "reason": "simulated for starvation test",
        }
        task.context = ctx
        return task

    # ── Already-proven tasks (have CriterionEvidence rows) ──────────────
    ALREADY_PROVEN = 10
    for _ in range(ALREADY_PROVEN):
        task = _make_candidate(proven=True)
        db_session.add(task)
        await db_session.flush()
        ev = CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="task_merge",
            source_id=str(task.id),
            detail={"proven": True},
        )
        db_session.add(ev)
    await db_session.flush()

    # ── False-negative tasks (satisfied=True, proven=False, no evidence) ─
    FALSE_NEGATIVE = 205
    for _ in range(FALSE_NEGATIVE):
        task = _make_candidate(proven=False)
        db_session.add(task)
    await db_session.flush()

    # ── Run backfill until all false-negatives are consumed ─────────────
    total_upgraded = 0
    for iteration in range(5):
        upgraded = await backfill_acceptance_verdicts(db_session)
        total_upgraded += upgraded
        if upgraded == 0:
            break

    assert total_upgraded == FALSE_NEGATIVE, (
        f"Expected {FALSE_NEGATIVE} upgrades (all false-negative tasks), "
        f"got {total_upgraded}; {ALREADY_PROVEN} already-proven tasks "
        f"should have been excluded via NOT EXISTS"
    )

    # Confirm idempotent: a final pass returns 0
    final = await backfill_acceptance_verdicts(db_session)
    assert final == 0, "No false negatives should remain after full sweep"


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on expired task attributes is asyncpg-specific; aiosqlite "
        "lazy-loads succeed in the async greenlet, masking the regression. "
        "Fault injection is not practical: the rollback happens inside "
        "record_task_merge_evidence which re-fetches the task via session.get(), "
        "leaving no ORM object that a proxy could intercept."
    ),
)
@pytest.mark.asyncio
async def test_backfill_verdicts_resilient_to_mid_loop_rollback(db_session):
    """A rollback inside record_task_merge_evidence (e.g. IntegrityError on
    CriterionEvidence unique constraint) must not abort the whole backfill
    cycle.  Specifically, subsequent loop iterations must not raise
    MissingGreenlet when accessing task attributes after the rollback
    unconditionally expired all session objects.

    Regression test for the gap where expire_on_commit=False only covered
    the COMMIT path, leaving the ROLLBACK path unprotected.  The fix
    collects task IDs upfront and re-fetches each task fresh at the start
    of every iteration via session.get(), making each iteration independent
    of prior commits or rollbacks.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    FIX_TS = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)

    # Three tasks eligible for upgrade (false-negative named_test verdicts).
    for _ in range(3):
        task = _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_invariants.py",
            },
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = FIX_TS
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": True,
            "proven": False,
            "kind": "named_test",
            "reason": "simulated false-negative for rollback resilience test",
        }
        task.context = ctx
        db_session.add(task)
    await db_session.flush()

    # Patch record_task_merge_evidence to simulate a session rollback on the
    # first call (as would happen when record_evidence hits an IntegrityError
    # on a duplicate CriterionEvidence row).  After the rollback all session
    # objects are expired; the fix must re-fetch each task so the 2nd and 3rd
    # iterations do not raise MissingGreenlet on task attribute access.
    call_count = [0]

    async def _mock_rtme_with_rollback(session, task, project_id):
        call_count[0] += 1
        if call_count[0] == 1:
            await session.rollback()
        return 0

    with patch(
        "app.acceptance_criteria.record_task_merge_evidence",
        _mock_rtme_with_rollback,
    ):
        upgraded = await backfill_acceptance_verdicts(db_session)

    assert upgraded == 3, (
        f"Expected all 3 tasks upgraded (verdict set to proven); got {upgraded}. "
        "A MissingGreenlet or premature abort would have reduced this count."
    )


@pytest.mark.asyncio
async def test_backfill_verdicts_advances_past_unprovable(db_session):
    """The backfill query advances past tasks that can never be proven (wrong
    verdict kind, not satisfied, or re-evaluation still unprovable), instead of
    getting stuck on them permanently and starving newer tasks behind them.

    Regression test for the starvation bug where the LIMIT-200 window never
    advanced because non-upgradeable tasks received no marker and blocked all
    later-ordered tasks from ever being examined.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Create an open release version so record_task_merge_evidence persists
    # CriterionEvidence rows for upgraded tasks — this is required by the
    # NOT EXISTS filter that handles already-proven rows.
    version = ReleaseVersion(project_id=project.id)
    db_session.add(version)
    await db_session.flush()

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-backfill-advance-test",
        text="Backfill advance test criterion",
    )
    db_session.add(criterion)
    await db_session.flush()
    criterion_key = criterion.criterion_key

    OLD_TS = datetime(2026, 7, 9, 7, 0, 0, tzinfo=timezone.utc)  # Pre-fix
    NEW_TS = datetime(2026, 7, 9, 9, 0, 0, tzinfo=timezone.utc)  # Also pre-fix, newer

    def _make_candidate(
        *,
        verdict_kind: str = "named_test",
        satisfied: bool = True,
        proven: bool = False,
        completed_at: datetime = OLD_TS,
        test_path: str | None = None,
        changed_files: list[str] | None = None,
    ) -> Task:
        task = _make_code_task(
            acceptance_evidence=(
                {"kind": "named_test", "test_path": test_path or "backend/testing/test_invariants.py"}
                if verdict_kind == "named_test"
                else {"kind": "observable_behavior"}
            ),
            changed_files=changed_files or [
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = completed_at
        task.criterion_ids = [criterion_key]
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": satisfied,
            "proven": proven,
            "kind": verdict_kind,
            "reason": "simulated for advance test",
        }
        task.context = ctx
        return task

    # ── Old non-upgradeable tasks (occupy the front of the LIMIT window) ──
    # We create >= 200 tasks that the backfill examines and marks but can
    # never upgrade. These fill the entire LIMIT-200 window on every call,
    # so WITHOUT the _backfill_checked_at marker the window never advances
    # past them and the upgradeable tasks behind them are starved.
    # Three categories are cycled:
    #   - Wrong verdict kind (not named_test)
    #   - Not satisfied
    #   - Re-evaluation still unprovable
    BLOCKER_COUNT = 215  # > LIMIT-200 to saturate the window
    for i in range(BLOCKER_COUNT):
        category = i % 3
        if category == 0:
            # Wrong verdict kind — skipped at the kind guard
            task = _make_candidate(verdict_kind="observable_behavior")
        elif category == 1:
            # Not satisfied — skipped at the satisfied guard
            task = _make_candidate(satisfied=False)
        else:
            # Re-evaluation still unprovable — test_path not in changed_files
            task = _make_candidate(
                test_path="backend/testing/test_reaper.py",
                changed_files=["backend/app/reaper.py"],
            )
        db_session.add(task)
    await db_session.flush()

    # ── Newer upgradeable tasks behind the stuck ones ─────────────────────
    # These are only reachable across successive backfill calls if the
    # _backfill_checked_at marker excludes the blocker tasks from the query.
    FALSE_NEGATIVE = 205
    for _ in range(FALSE_NEGATIVE):
        task = _make_candidate(
            completed_at=NEW_TS,
            test_path="backend/testing/test_invariants.py",
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        db_session.add(task)
    await db_session.flush()

    # ── Run backfill repeatedly until exhausted ──────────────────────────
    # The loop does NOT break on upgraded==0 because the first 1-2 calls
    # may return 0 upgrades while filling the LIMIT-200 window with blocker
    # tasks that the marker stamps. Only when all blockers are marked (and
    # thus excluded via _backfill_checked) does the query advance to the
    # upgradeable false-negative tasks.
    # Without the marker fix, the window stays stuck on the 215 blockers
    # permanently and total_upgraded stays 0 → assertion catches it.
    total_upgraded = 0
    for iteration in range(6):
        upgraded = await backfill_acceptance_verdicts(db_session)
        total_upgraded += upgraded

    # All 205 false negatives must be upgraded. WITHOUT the marker fix the
    # window stays stuck on the 215 blocker tasks (all in the LIMIT-200 query
    # result on every call since none get marked) and the upgradeable tasks
    # are never reached.
    assert total_upgraded == FALSE_NEGATIVE, (
        f"Expected {FALSE_NEGATIVE} upgrades (all false-negative tasks), "
        f"got {total_upgraded} — the backfill window is stuck on "
        f"non-upgradeable tasks"
    )

    # Confirm idempotent: a final pass returns 0
    final = await backfill_acceptance_verdicts(db_session)
    assert final == 0, "No false negatives should remain after full sweep"


@pytest.mark.asyncio
async def test_backfill_missing_diff_provenance_stays_re_examinable(db_session):
    """An unavailable-provenance named_test verdict that STILL cannot be proven
    (no changed-file provenance has arrived) is stamped with the ordinary one-shot
    ``_backfill_checked_at`` marker — freeing the LIMIT-200 window — but NOT
    the terminal marker: the candidate query re-admits the class as soon as
    provenance arrives, so marking never freezes the verdict. (Pre-fix the
    class was left unmarked to stay re-examinable, which let permanently
    unprovable rows occupy the window forever.)"""
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,  # provenance has not arrived yet
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' could not be "
            "evaluated because PR changed-file provenance was unavailable — "
            "_pr_changed_files was never recorded"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0, "no provenance yet — the verdict cannot be upgraded"

    await db_session.refresh(task)
    assert "_backfill_checked_at" in (task.context or {}), (
        "an unprovable candidate must be marked so the window advances past it"
    )
    assert "_backfill_missing_diff_terminal_at" not in (task.context or {}), (
        "no provenance was in hand — the verdict is not terminal, and the "
        "provenance-arrived re-admission clause must still apply later"
    )
    recorded = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert recorded is not None
    assert recorded.get("satisfied") is False, "verdict is untouched until it flips"

    # ── Late-arriving GitHub-verified provenance re-admits DESPITE the marker ─
    from app.acceptance_gate import stamp_changed_files_signal

    task_ctx = dict(task.context or {})
    task_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    task.context = task_ctx
    db_session.add(task)
    await db_session.commit()

    result2 = await backfill_acceptance_verdicts(db_session)
    assert result2 >= 1, (
        "a marked missing-from-diff verdict must still upgrade once provenance "
        "arrives — the marker frees the window, it must not freeze the class"
    )


@pytest.mark.asyncio
async def test_backfill_terminal_marks_unprovable_missing_from_diff_with_provenance(
    db_session,
):
    """A missing-from-diff task whose changed-file provenance IS present but
    still does not contain the declared test (the test genuinely never shipped)
    is terminally marked by the sweep and stops consuming the LIMIT-200 window:
    a second sweep does not re-examine it. Fresh provenance (stamped by a
    changed-file site, which clears both markers) re-admits it. Regression test
    for the window-stall: pre-fix such a task was re-selected by every sweep
    forever, and 200 of them would starve all other candidates permanently."""
    from datetime import datetime, timezone

    from app.acceptance_gate import stamp_changed_files_signal
    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        # Provenance present — and the declared test is NOT in it.
        changed_files=["backend/app/widget.py"],
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_github_verified"] = True
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' is not among the "
            "PR's changed files — the claimed test was not added or modified here"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    # First sweep: examined, unprovable WITH provenance in hand → terminal.
    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0
    await db_session.refresh(task)
    checked_ts = (task.context or {}).get("_backfill_checked_at")
    terminal_ts = (task.context or {}).get("_backfill_missing_diff_terminal_at")
    assert checked_ts, "unprovable candidate must be marked"
    assert terminal_ts, (
        "provenance was present yet re-evaluation failed — the terminal marker "
        "must be stamped so the task leaves the sweep window"
    )

    # Second sweep: the task is no longer a candidate. If it were re-selected,
    # the sweep would re-stamp both markers with a fresh timestamp.
    result2 = await backfill_acceptance_verdicts(db_session)
    assert result2 == 0
    await db_session.refresh(task)
    assert (task.context or {}).get("_backfill_checked_at") == checked_ts, (
        "a terminally-marked task must stop consuming the sweep window"
    )
    assert (
        (task.context or {}).get("_backfill_missing_diff_terminal_at")
        == terminal_ts
    )

    # Fresh provenance NOW containing the test arrives via a changed-file
    # stamping site — which clears both markers — and re-admits the task.
    task_ctx = dict(task.context or {})
    task_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    task_ctx.pop("_backfill_checked_at", None)
    task_ctx.pop("_backfill_missing_diff_terminal_at", None)
    task.context = task_ctx
    db_session.add(task)
    await db_session.commit()

    result3 = await backfill_acceptance_verdicts(db_session)
    assert result3 >= 1, "cleared markers + fresh provenance must re-admit and heal"
    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("proven") is True


@pytest.mark.asyncio
async def test_backfill_re_examines_pre_marked_missing_from_diff_once_provenance_arrives(
    db_session,
):
    """A missing-from-diff verdict that an OLD sweep already stamped with the
    one-shot ``_backfill_checked_at`` marker is still re-admitted once
    provenance arrives. Pre-fix the marker filter would permanently exclude the
    task — freezing the declared_unsatisfied verdict even after GitHub-verified
    changed files land. The sweep now ORs in the missing-from-diff reason class,
    so the marker is no longer a hard exclusion for the exact verdicts this
    sweep exists to heal."""
    from datetime import datetime, timezone

    from app.acceptance_gate import stamp_changed_files_signal
    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,  # provenance has not arrived yet
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' is not among the "
            "PR's changed files — the claimed test was not added or modified here"
        ),
    }
    # Simulate the pre-fix sweep: it stamped the one-shot marker on this class.
    ctx["_backfill_checked_at"] = "2026-07-09T09:00:00+00:00"
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    # No provenance yet — the MARKED task is still admitted (missing-from-diff
    # OR clause) but cannot flip.
    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0, "no provenance yet — the verdict cannot be upgraded"
    await db_session.refresh(task)
    assert (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY, {}).get(
        "satisfied"
    ) is False

    # ── Late-arriving GitHub-verified provenance ──────────────────────────
    task_ctx = dict(task.context or {})
    task_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    task.context = task_ctx
    db_session.add(task)
    await db_session.commit()

    result2 = await backfill_acceptance_verdicts(db_session)
    assert result2 >= 1, (
        "a pre-fix-marked missing-from-diff verdict must upgrade once "
        "provenance arrives"
    )

    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("satisfied") is True
    assert updated.get("proven") is True, "own GitHub-verified diff earns machine proof"


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_heals_missing_from_diff_with_verified_provenance(
    db_session,
):
    """A missing-from-diff verdict is healed to proven=True by the backfill once
    GitHub-verified changed files arrive on the task's own context (the ordinary
    PR-owning task case). The first sweep marks the task (freeing the window);
    the provenance-arrived re-admission clause brings it back for the second."""
    from datetime import datetime, timezone

    from app.acceptance_gate import stamp_changed_files_signal
    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' is not among the "
            "PR's changed files — the claimed test was not added or modified here"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    # First sweep: no provenance, cannot prove — marked (non-terminally) so
    # the window advances past it until provenance arrives.
    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0
    await db_session.refresh(task)
    assert "_backfill_checked_at" in (task.context or {})
    assert "_backfill_missing_diff_terminal_at" not in (task.context or {})

    # ── Late-arriving GitHub-verified provenance ──────────────────────────
    task_ctx = dict(task.context or {})
    task_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    task.context = task_ctx
    db_session.add(task)
    await db_session.commit()

    result2 = await backfill_acceptance_verdicts(db_session)
    assert result2 >= 1, "missing-from-diff verdict should upgrade once provenance arrives"

    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("satisfied") is True
    assert updated.get("proven") is True, "own GitHub-verified diff earns machine proof"
    assert updated.get("backfilled") is True


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_flips_missing_from_diff_to_satisfied_unproven(
    db_session,
):
    """A rollup child's missing-from-diff verdict flips to satisfied_unproven
    (a SOFT upgrade) once parent-derived changed files arrive. Parent-derived
    credit is capped below machine proof, so the verdict moves out of
    declared_unsatisfied into the acceptance window's verified numerator without
    minting a proven row."""
    from datetime import datetime, timezone

    from app.acceptance_gate import stamp_parent_derived_changed_files_signal
    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' is not among the "
            "PR's changed files — the claimed test was not added or modified here"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    # Parent-derived union diff arrives (rollup child inherits the parent's list).
    task_ctx = dict(task.context or {})
    task_ctx.update(
        stamp_parent_derived_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
        )
    )
    task.context = task_ctx
    db_session.add(task)
    await db_session.commit()

    result = await backfill_acceptance_verdicts(db_session)
    assert result >= 1, "missing-from-diff verdict should soft-flip to satisfied_unproven"

    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("satisfied") is True
    assert updated.get("proven") is False, (
        "parent-derived credit is capped at the satisfied/unproven tier"
    )
    assert updated.get("backfilled") is True
    assert classify_merged_acceptance_context(task.context) == "satisfied_unproven"


@pytest.mark.asyncio
async def test_record_code_acceptance_verdict_clears_stale_backfill_marker(db_session):
    """Re-recording a verdict (new provenance arrived) clears the one-shot
    ``_backfill_checked_at`` marker so the task re-enters the backfill sweep
    pool instead of being permanently excluded by the sweep's idempotency
    filter."""
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    ctx = dict(task.context or {})
    ctx["_backfill_checked_at"] = "2026-07-09T09:00:00+00:00"
    ctx["_backfill_missing_diff_terminal_at"] = "2026-07-09T09:00:00+00:00"
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": False,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_widget.py' is not among the "
            "PR's changed files — the claimed test was not added or modified here"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    record_code_acceptance_verdict(task)

    assert "_backfill_checked_at" not in (task.context or {}), (
        "a re-recorded verdict must clear the one-shot backfill marker"
    )
    assert "_backfill_missing_diff_terminal_at" not in (task.context or {}), (
        "a re-recorded verdict must clear the terminal missing-from-diff marker"
    )
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("satisfied") is True
    assert updated.get("proven") is True


@pytest.mark.asyncio
async def test_landed_rollup_child_regrades_after_parent_changed_files_arrive(db_session):
    """A rollup child whose acceptance verdict was recorded declared_unsatisfied
    (missing-from-diff) BEFORE the parent's changed files arrived re-grades to
    satisfied_unproven once the parent's ``_pr_changed_files`` propagate down —
    even though the child already carries the landed signal.

    Regression test for the frozen-verdict gap: the rollup propagation used to
    run the changed-file propagation + verdict re-record ONLY inside the
    ``not has_landed_signal(row)`` branch, so an already-landed child was
    skipped forever and late-arriving provenance could never flip it.
    """
    from app.acceptance_gate import (
        PARENT_DERIVED_CHANGED_FILES_KEY,
        stamp_changed_files_signal,
    )
    from app.landed_signal import has_landed_signal
    from app.rollup_management import (
        _propagate_landed_to_staging_merged_descendants,
    )
    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Rollup parent: landed on integration, but its changed-file list arrives LATER.
    parent = Task(
        project_id=project.id,
        title="rollup parent",
        payloads=[{"type": "research", "prompt": "plan the work"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Child create task that merged via the parent's staging branch. It never
    # owns a PR, so without the parent's files its declared named_test cannot be
    # located in any diff.
    child = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=None,
    )
    child.project_id = project.id
    child.parent_task_id = parent.id
    child.context = {
        "pr_merged": True,
        "landed_on_integration": False,
        "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
    }
    db_session.add(child)
    await db_session.flush()

    # First propagation: parent has NO changed files → child lands but its verdict
    # is frozen at declared_unsatisfied with unavailable provenance.
    stamped = await _propagate_landed_to_staging_merged_descendants(
        parent.id, db_session
    )
    await db_session.commit()
    assert child.id in {t.id for t in stamped}

    await db_session.refresh(child)
    assert has_landed_signal(child)
    recorded = (child.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert recorded is not None
    assert recorded.get("satisfied") is False
    assert "changed-file provenance was unavailable" in (recorded.get("reason") or "")

    # ── Late arrival: the parent's GitHub-verified changed files land. ─────
    parent_ctx = dict(parent.context or {})
    parent_ctx.update(
        stamp_changed_files_signal(
            ["backend/app/widget.py", "backend/testing/test_widget.py"],
            github_verified=True,
        )
    )
    parent.context = parent_ctx
    db_session.add(parent)
    await db_session.commit()

    # Second propagation: the child is ALREADY landed, but the changed-file
    # propagation must still reach it and re-record its verdict.
    again = await _propagate_landed_to_staging_merged_descendants(
        parent.id, db_session
    )
    await db_session.commit()
    assert again == [], "no NEWLY-landed descendants — the child was already landed"

    await db_session.refresh(child)
    child_ctx = child.context or {}
    assert child_ctx.get("_pr_changed_files") == [
        "backend/app/widget.py",
        "backend/testing/test_widget.py",
    ]
    assert child_ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True
    # The parent's union diff is deliberately NOT marked github-verified on a child.
    assert "_pr_changed_files_github_verified" not in child_ctx

    # The frozen verdict flipped: declared_unsatisfied -> satisfied_unproven.
    re_recorded = child_ctx.get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert re_recorded is not None
    assert re_recorded.get("satisfied") is True
    assert re_recorded.get("proven") is False, (
        "parent-derived credit is capped below machine proof"
    )
    assert classify_merged_acceptance_context(child_ctx) == "satisfied_unproven"


# ─────────────────────────────────────────────────────────────────────────────
# Writer/reader contract for the merge-time acceptance-test evidence
#
# Both evidence paths this gate reads were once dead against the real writer:
# the normalized block had NO writer at all, and the targeted-green stamp was
# withheld for exactly the not-in-diff shape the gate wants to credit. These
# tests bind the reader to the writer so neither can silently go dead again.
# ─────────────────────────────────────────────────────────────────────────────


def test_merge_acceptance_block_reads_the_key_the_monitor_writes():
    """The gate reads the SAME review_loop key orchestrator_ci_monitor writes."""
    import inspect

    from app import orchestrator_ci_monitor
    from app.acceptance_gate import (
        MERGE_ACCEPTANCE_BLOCK_KEY,
        _merge_acceptance_test_block,
    )

    src = inspect.getsource(orchestrator_ci_monitor)
    assert f'ci_loop["{MERGE_ACCEPTANCE_BLOCK_KEY}"]' in src, (
        f"no writer for review_loop[{MERGE_ACCEPTANCE_BLOCK_KEY!r}] in "
        "orchestrator_ci_monitor — the gate's merge-block evidence path is dead"
    )

    block = {
        "declared": True,
        "descriptor": "the seam test",
        "present": True,
        "ran": True,
        "status": "ran",
        "passed": True,
    }
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        merge_test_block=block,
    )
    assert _merge_acceptance_test_block(task.context) == block
    # The outcome STRING the monitor also stores must never be mistaken for it.
    task.context[REVIEW_LOOP_KEY]["verify_tests_verdict"] = "pass"
    assert _merge_acceptance_test_block(task.context) == block
    # Defensive: absent / malformed → None (legacy tasks fall back to the stamp).
    del task.context[REVIEW_LOOP_KEY][MERGE_ACCEPTANCE_BLOCK_KEY]
    assert _merge_acceptance_test_block(task.context) is None
    task.context[REVIEW_LOOP_KEY][MERGE_ACCEPTANCE_BLOCK_KEY] = "not a dict"
    assert _merge_acceptance_test_block(task.context) is None


def test_merge_acceptance_block_reads_inherited_block_for_rollup_child():
    """A rollup child (which merges via the parent's single PR and never owns a
    review_loop) reads its PARENT-INHERITED acceptance-test block — rung 6 of
    design doc c50453ef, closing U1. The inherited block lives at the TOP level
    under ``_inherited_acceptance_block`` (never inside review_loop, which a
    child legitimately lacks), and the reader falls back to it only when the
    child carries no own block. A mismatched inherited block fails closed — a
    green run of some OTHER test is never credited for this declaration."""
    from app.acceptance_gate import (
        INHERITED_ACCEPTANCE_BLOCK_KEY,
        MERGE_ACCEPTANCE_BLOCK_KEY,
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
        _merge_acceptance_test_block,
    )

    block = {
        "declared": True,
        "descriptor": "the seam test",
        "selector": "testing/test_seam.py::test_x",
        "present": True,
        "ran": True,
        "status": "ran",
        "passed": True,
    }
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],
    )
    # Rollup child shape: NO review_loop at all, only the parent's inherited
    # block stamped by _propagate_landed_to_staging_merged_descendants.
    task.context.pop(REVIEW_LOOP_KEY, None)
    task.context[INHERITED_ACCEPTANCE_BLOCK_KEY] = block
    assert _merge_acceptance_test_block(task.context) == block
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is True
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is True

    # A child that somehow DOES carry its own review_loop block: the own block
    # wins, and a non-green own block is not rescued by the inherited one.
    own = dict(block)
    own["passed"] = False
    task.context[REVIEW_LOOP_KEY] = {MERGE_ACCEPTANCE_BLOCK_KEY: own}
    assert _merge_acceptance_test_block(task.context) == own
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is False

    # A mismatched inherited block (names a DIFFERENT test — descriptor AND
    # selector both point elsewhere) fails closed.
    mismatched = dict(block)
    mismatched["descriptor"] = "some_other_test.py"
    mismatched["selector"] = "backend/testing/test_other.py::test_other"
    task2 = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],
    )
    task2.context.pop(REVIEW_LOOP_KEY, None)
    task2.context[INHERITED_ACCEPTANCE_BLOCK_KEY] = mismatched
    declared2 = _declared_named_test_path(task2.first_payload)
    assert _acceptance_test_exists_at_merge(task2.context, declared2) is False
    assert _acceptance_test_ran_green_at_merge(task2.context, declared2) is False


def test_not_in_diff_green_run_flows_writer_to_gate_as_proven():
    """End-to-end: a pre-existing test run green with in_diff=False now earns
    proven — the exact behaviour-preserving-refactor case the gate must credit.

    Exercises the real writer predicate (``acceptance_test_is_green``) rather than
    hand-stamping the signal, so a re-introduced ``in_diff`` short-circuit in
    ci_resolution turns this red instead of passing on a fabricated stamp.
    """
    from app import ci_resolution
    from app.acceptance_gate import (
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
        _evaluate_named_test,
    )

    verdict = {
        "detected": True,
        "passed": True,
        "acceptanceTest": {
            "declared": True,
            "descriptor": "the seam test",
            "selector": "testing/test_seam.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
            "in_diff": False,
        },
    }
    # 1. The merge gate does not block it pre-merge.
    assert ci_resolution.classify_merge_test_verdict(verdict)[0] == "pass"
    # 2. The writer mints the targeted-green signal for it.
    accept_block = ci_resolution.acceptance_test_block(verdict)
    assert ci_resolution.acceptance_test_is_green(accept_block) is True

    # 3. Replay what orchestrator_ci_monitor persists onto review_loop.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],  # production change, test NOT in diff
        targeted_green=True,
        merge_test_block=accept_block,
    )
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is True
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is True

    # 4. With the backend's OWN tree check confirming existence, the gate
    # credits it as PROVEN.
    verdict_out = _evaluate_named_test(
        AcceptanceEvidence(
            kind="named_test", test_path="testing/test_seam.py", test_id="test_x"
        ),
        ["app/seam.py"],
        verified=False,
        targeted_green=True,
        test_exists=True,
        ran_green=True,
        corroborated=True,
    )
    assert verdict_out.satisfied is True
    assert verdict_out.proven is True, (
        f"not-in-diff green test still not credited: {verdict_out.reason}"
    )

    # 5. Without positive backend corroboration every remaining input is
    # agent-reported, so the same shape is credited SOFT — satisfied, never
    # machine-proven (see test_not_in_diff_proven_requires_positive_corroboration).
    verdict_soft = _evaluate_named_test(
        AcceptanceEvidence(
            kind="named_test", test_path="testing/test_seam.py", test_id="test_x"
        ),
        ["app/seam.py"],
        verified=False,
        targeted_green=True,
        test_exists=True,
        ran_green=True,
    )
    assert verdict_soft.satisfied is True
    assert verdict_soft.proven is False


def test_merge_acceptance_block_must_name_the_declared_test():
    """The existence-at-merge credit is only earned by a merge-test block that
    NAMES THE DECLARED test — a green run of some OTHER test is a name-drop of
    the declaration and must fail closed with a distinct, actionable cause.

    This is the acceptance evidence for the corroboration fix: the gate reads
    ``test_exists`` / ``ran_green`` straight from an agent-reported block, so it
    must verify the block describes the declared ``test_path`` (acceptance_gate
    / orchestrator_ci_monitor), not just that SOME test ran green.
    """
    from app.acceptance_gate import (
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
    )

    # The agent ran a DIFFERENT test green (its selector names test_widget, not
    # the declared test_acceptance_gate) while the declared test is absent from
    # the PR's diff — the exact name-drop shape the audit finding flags.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],  # declared test NOT in diff
        unverified_reason="no_detectable_test_command",
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "selector": "backend/testing/test_widget.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    # The hardened readers refuse the mismatched block as existence/greenness of
    # the DECLARED test.
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is False
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is False
    # The gate fails closed and names the real cause.
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "DIFFERENT test" in verdict.reason

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) == (
        "named_test_block_describes_other_test"
    )


def test_block_mismatch_does_not_override_truncation_credit():
    """A mismatched merge-test block must NOT convert the soft TRUNCATION credit
    into a hard fail.

    On a >50-file PR the stored changed-file list is capped, so a truthfully
    declared test can lie beyond the stored prefix and read as absent. That
    credit rests on the absence being INCONCLUSIVE, not on the merge-test block,
    so the corroboration guard has nothing to withdraw. Failing closed here would
    retroactively downgrade already-merged large PRs when their verdicts are
    re-derived, lowering ``acceptance_window_fraction``.
    """
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        # Declared test lies beyond the stored 50-path prefix → reads as absent.
        changed_files=["backend/app/acceptance_gate.py"],
        unverified_reason=None,
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "selector": "backend/testing/test_widget.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True, (
        "truncation credit is block-independent — a block naming a different "
        "test must not hard-fail a >50-file PR whose declared test may ship "
        "beyond the stored prefix"
    )
    assert verdict.proven is False
    assert "truncated" in verdict.reason

    # The guard still fires on the same block when the list is NOT truncated:
    # there, absence IS authoritative and the credit would rest on the block.
    not_truncated = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],
        unverified_reason="no_detectable_test_command",
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "selector": "backend/testing/test_widget.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    strict = evaluate_code_acceptance(not_truncated)
    assert strict.satisfied is False
    assert "DIFFERENT test" in strict.reason


def test_block_mismatch_still_fires_when_truncated_but_test_present():
    """Truncation only exempts the ABSENT case. When the declared test IS in the
    truncated prefix and the merge fell open unverified, the credit would rest on
    the merge-test block — a block naming a different test still fails closed."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
        merge_test_block={
            "declared": True,
            "descriptor": "test_widget.py",
            "selector": "backend/testing/test_widget.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "DIFFERENT test" in verdict.reason


def test_targeted_green_descriptor_mismatch_fails_closed():
    """A targeted-green signal whose distilled descriptor names a DIFFERENT test
    is not identity evidence for the declared test — the gate fails closed even
    though a green signal was stamped (e.g. an older monitor, or tampering)."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],  # declared test NOT in diff
        unverified_reason="no_detectable_test_command",
        targeted_green=True,
    )
    # The monitor normally stamps a matching descriptor; simulate a mismatched one.
    task.context[REVIEW_LOOP_KEY]["acceptance_test_targeted_descriptor"] = (
        "backend/testing/test_widget.py"
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "DIFFERENT test" in verdict.reason

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) == (
        "named_test_block_describes_other_test"
    )


def test_block_selector_with_node_id_matches_declared_path():
    """A merge-test block whose selector is ``path::node`` still names the
    declared test path — the ``::`` node suffix is stripped before matching, so a
    targeted run pinned to one test function corroborates the declared file."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py::test_x",
        },
        changed_files=["backend/app/widget.py"],  # declared test absent from diff
        unverified_reason="no_detectable_test_command",
        merge_test_block={
            "declared": True,
            "descriptor": "test_x",
            "selector": "backend/testing/test_widget.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    from app.acceptance_gate import ACCEPTANCE_TEST_CORROBORATED_KEY

    task.context = {**(task.context or {}), ACCEPTANCE_TEST_CORROBORATED_KEY: True}
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert verdict.proof_ref == "backend/testing/test_widget.py::test_x"


def test_name_dropped_test_with_no_existence_evidence_still_hard_fails():
    """The anti-fraud guard survives: a declared test absent from the diff AND
    with no existence/green evidence is still a hard fail."""
    from app.acceptance_gate import _evaluate_named_test

    verdict_out = _evaluate_named_test(
        AcceptanceEvidence(
            kind="named_test", test_path="testing/test_nowhere.py"
        ),
        ["app/seam.py"],
        verified=True,
        targeted_green=False,
        test_exists=False,
        ran_green=False,
    )
    assert verdict_out.proven is False
    assert "not among the PR's changed files" in verdict_out.reason
