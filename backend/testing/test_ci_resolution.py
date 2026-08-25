"""Tests for backend/app/ci_resolution.py — the strict orchestrator-side
CI verdict resolver that replaces the lenient ``get_check_runs`` default on
the PR-merge hot path.

The lenient default treats a commit with zero check-runs as a passing build
so the orchestrator's CI-wait does not hang on no-CI repos.  That is wrong
for PR-driven CI configs where the ``pull_request`` event runs on GitHub's
synthetic merge-commit SHA rather than the branch HEAD: the branch HEAD then
carries zero check-runs while the real verdict lives on the PR head commit
reachable via ``commits/{sha}/pulls``.  The strict resolver inspects the
branch HEAD first, falls back to the PR head, and refuses to mark CI green
purely from the absence of a signal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import ci_resolution
from app.models import Project


def _proj(build_config=None):
    return Project(name="t", user_id="u", build_config=build_config)


# proceed_on_no_checks / requires_ci_checks — greenfield no-CI repo merge policy


def test_requires_ci_checks_defaults_true():
    assert ci_resolution.requires_ci_checks(_proj()) is True
    assert ci_resolution.requires_ci_checks(_proj({"other": 1})) is True


def test_requires_ci_checks_false_when_declared():
    assert (
        ci_resolution.requires_ci_checks(_proj({"requires_ci_checks": False})) is False
    )


def test_proceed_on_no_checks_ci_repo_non_staging_keeps_polling():
    # CI-gated repo: a definitive no_checks on a trunk base must NOT proceed —
    # for a CI repo a missing check means "not registered yet", so keep polling.
    assert ci_resolution.proceed_on_no_checks(_proj(), "main") is False


def test_proceed_on_no_checks_staging_base_proceeds():
    # Staging-branch rollups have CI path-filtered out by design → proceed.
    assert ci_resolution.proceed_on_no_checks(_proj(), "research/cycle-1") is True
    assert ci_resolution.proceed_on_no_checks(_proj(), "plan/cycle-1") is True


def test_proceed_on_no_checks_relaxed_ci_capable_repo_keeps_required_branch_strict():
    # A CI-capable repo (has_ci=True) that relaxed requires_ci_checks — to skip
    # per-PR CI on its integration branch — must STILL gate its
    # ci_required_branches members (default ["main"], the promotion target)
    # strictly: a no_checks on the promotion PR must never merge on absence of
    # signal. The integration branch (automain), whose PRs now legitimately carry
    # zero checks, proceeds. This closes the promotion race (Blueprint's config).
    p = _proj({"has_ci": True, "requires_ci_checks": False})
    assert ci_resolution.proceed_on_no_checks(p, "main") is False  # required → strict
    assert ci_resolution.proceed_on_no_checks(p, "automain") is True  # integration → proceed


def test_proceed_on_no_checks_ci_required_branches_override():
    # An explicit ci_required_branches override controls which bases stay strict
    # (only for a confirmed-CI repo that relaxed requires_ci_checks).
    p = _proj(
        {
            "has_ci": True,
            "requires_ci_checks": False,
            "ci_required_branches": ["main", "release"],
        }
    )
    assert ci_resolution.proceed_on_no_checks(p, "main") is False
    assert ci_resolution.proceed_on_no_checks(p, "release") is False
    assert ci_resolution.proceed_on_no_checks(p, "automain") is True
    # Empty override → no branch is forced strict; relaxed repo proceeds anywhere.
    p_open = _proj(
        {"has_ci": True, "requires_ci_checks": False, "ci_required_branches": []}
    )
    assert ci_resolution.proceed_on_no_checks(p_open, "main") is True


def test_proceed_on_no_checks_required_branch_override_scoped_to_has_ci_true():
    # The required-branch override is scoped to has_ci is True, so external no-CI
    # repos are untouched and never wedged into polling on their real base:
    #   * has_ci=False → short-circuits to proceed (e.g. stsbot2).
    #   * has_ci=None (undetermined) → falls back to requires_ci_checks, NOT forced
    #     strict — an external no-CI repo merging to main still proceeds.
    p_false = _proj({"has_ci": False, "requires_ci_checks": False})
    assert ci_resolution.proceed_on_no_checks(p_false, "main") is True
    p_none = _proj({"requires_ci_checks": False})  # has_ci undetermined
    assert ci_resolution.proceed_on_no_checks(p_none, "main") is True


def test_proceed_on_no_checks_has_ci_false_proceeds_even_when_requires_ci_stale():
    # Primary fix: has_ci=False is authoritative — the repo has zero CI workflows
    # and can never produce check-runs.  A stale requires_ci_checks=True must NOT
    # strand a rollup PR into main by polling forever.
    p = _proj({"has_ci": False, "requires_ci_checks": True})
    assert ci_resolution.proceed_on_no_checks(p, "main") is True
    assert ci_resolution.proceed_on_no_checks(p, "automain") is True
    assert ci_resolution.proceed_on_no_checks(p, None) is True


def test_proceed_on_no_checks_has_ci_true_still_polls():
    # Inverse guard: a repo with actual CI workflows must keep polling when no
    # checks are registered yet — "not registered yet", not "will never run".
    p = _proj({"has_ci": True})
    assert ci_resolution.proceed_on_no_checks(p, "main") is False


def test_proceed_on_no_checks_has_ci_none_falls_back_to_requires_ci():
    # has_ci=None (undetermined — detection was incomplete) does not trigger the
    # has_ci override path; falls back to requires_ci_checks as before.
    p_gated = _proj({"has_ci": None})
    assert ci_resolution.proceed_on_no_checks(p_gated, "main") is False
    p_ungated = _proj({"has_ci": None, "requires_ci_checks": False})
    assert ci_resolution.proceed_on_no_checks(p_ungated, "main") is True


def test_proceed_on_no_checks_none_project():
    assert ci_resolution.proceed_on_no_checks(None, "main") is False


# has_detectable_tests — does the stored build_config carry a runnable suite?


def test_has_detectable_tests_true_when_any_test_cmd_present():
    p = _proj(
        {
            "directories": [
                {"path": "backend/", "test_cmd": "python3 -m pytest -x -q"},
                {"path": "ui/", "test_cmd": ""},
            ]
        }
    )
    assert ci_resolution.has_detectable_tests(p) is True


def test_has_detectable_tests_false_when_no_directories():
    # Greenfield / empty repo: onboarding found no manifests → no test_cmd.
    assert ci_resolution.has_detectable_tests(_proj()) is False
    assert ci_resolution.has_detectable_tests(_proj({"directories": []})) is False


def test_has_detectable_tests_false_when_all_test_cmds_blank():
    p = _proj({"directories": [{"path": "x/", "test_cmd": "   "}, {"path": "y/"}]})
    assert ci_resolution.has_detectable_tests(p) is False


def test_has_detectable_tests_none_project():
    assert ci_resolution.has_detectable_tests(None) is False


# compute_merge_verification_verdict — read-only operator advisory. Must REUSE
# requires_ci_checks / has_detectable_tests and agree with them.


def test_verdict_ci_checks_when_requires_ci_default():
    # Default project (requires_ci_checks True) → merges wait for GitHub CI.
    p = _proj()
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict == {"state": "ci_checks", "test_commands": []}
    # Agrees with the gate predicate.
    assert ci_resolution.requires_ci_checks(p) is True


def test_verdict_ci_checks_even_with_detectable_tests():
    # requires_ci_checks True takes precedence over present test commands.
    p = _proj(
        {"directories": [{"path": "backend/", "test_cmd": "python3 -m pytest -x -q"}]}
    )
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict["state"] == "ci_checks"
    assert verdict["test_commands"] == []


def test_verdict_tests_gated_collects_commands():
    # requires_ci_checks False + detectable tests → tests_gated, only non-empty
    # test_cmd values returned with their path, stored order preserved.
    p = _proj(
        {
            "requires_ci_checks": False,
            "directories": [
                {"path": "backend/", "test_cmd": "python3 -m pytest -x -q"},
                {"path": "ui/", "test_cmd": "   "},
                {"path": "agent/", "test_cmd": "npm test"},
            ],
        }
    )
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict["state"] == "tests_gated"
    assert verdict["test_commands"] == [
        {"path": "backend/", "test_cmd": "python3 -m pytest -x -q"},
        {"path": "agent/", "test_cmd": "npm test"},
    ]
    # The verdict agrees with the gate predicates.
    assert ci_resolution.requires_ci_checks(p) is False
    assert ci_resolution.has_detectable_tests(p) is True


def test_verdict_tests_gated_handles_missing_path():
    p = _proj(
        {
            "requires_ci_checks": False,
            "directories": [{"test_cmd": "go test ./..."}],
        }
    )
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict["state"] == "tests_gated"
    assert verdict["test_commands"] == [{"path": "", "test_cmd": "go test ./..."}]


def test_verdict_unverified_when_no_tests():
    # requires_ci_checks False + no runnable suite → PRs merge UNVERIFIED.
    p = _proj({"requires_ci_checks": False, "directories": [{"path": "x/"}]})
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict == {"state": "unverified", "test_commands": []}
    assert ci_resolution.has_detectable_tests(p) is False


def test_verdict_none_project_is_stable_default_shape():
    # None project → CI-gated default, no raise (mirrors requires_ci_checks).
    verdict = ci_resolution.compute_merge_verification_verdict(None)
    assert verdict == {"state": "ci_checks", "test_commands": []}


def test_verdict_missing_build_config_is_safe():
    p = _proj(None)
    verdict = ci_resolution.compute_merge_verification_verdict(p)
    assert verdict == {"state": "ci_checks", "test_commands": []}


# should_run_no_ci_test_gate — the orchestrator's gating decision


def _no_ci_repo():
    return _proj(
        {
            "requires_ci_checks": False,
            "directories": [
                {"path": "backend/", "test_cmd": "python3 -m pytest -x -q"}
            ],
        }
    )


def test_gate_runs_for_external_no_ci_repo_with_tests_on_real_base():
    assert (
        ci_resolution.should_run_no_ci_test_gate(
            _no_ci_repo(), "main", gate_enabled=True
        )
        is True
    )


def test_gate_skipped_when_kill_switch_off():
    # Kill-switch off → fall back to legacy immediate-pass.
    assert (
        ci_resolution.should_run_no_ci_test_gate(
            _no_ci_repo(), "main", gate_enabled=False
        )
        is False
    )


def test_gate_skipped_on_internal_staging_base():
    # Staging rollups (research/plan) keep the immediate-pass — never run repo
    # tests on internal rollups, even for a no-CI project with detectable tests.
    p = _no_ci_repo()
    assert (
        ci_resolution.should_run_no_ci_test_gate(
            p, "research/cycle-1", gate_enabled=True
        )
        is False
    )
    assert (
        ci_resolution.should_run_no_ci_test_gate(p, "plan/cycle-1", gate_enabled=True)
        is False
    )


def test_gate_skipped_for_ci_gated_repo():
    # CI-gated repo (requires_ci_checks default True): the no_checks branch keeps
    # polling; the gate must never engage.
    p = _proj(
        {"directories": [{"path": "backend/", "test_cmd": "python3 -m pytest -x -q"}]}
    )
    assert (
        ci_resolution.should_run_no_ci_test_gate(p, "main", gate_enabled=True) is False
    )


def test_gate_skipped_when_no_detectable_tests():
    # No-CI repo but no runnable suite → fall back to proceed-without-checks
    # (absent-tests repos must still progress).
    p = _proj({"requires_ci_checks": False, "directories": []})
    assert (
        ci_resolution.should_run_no_ci_test_gate(p, "main", gate_enabled=True) is False
    )


def test_gate_none_project():
    assert (
        ci_resolution.should_run_no_ci_test_gate(None, "main", gate_enabled=True)
        is False
    )


# resolve_no_checks_escalation — the no-checks escalation backstop decision


def test_escalation_verify_tests_for_definitive_no_ci_with_suite():
    # has_ci=False + detectable suite → route through the verify_tests gate.
    p = _proj(
        {
            "requires_ci_checks": False,
            "has_ci": False,
            "directories": [{"path": ".", "test_cmd": "pytest"}],
        }
    )
    assert (
        ci_resolution.resolve_no_checks_escalation(p, "main", gate_enabled=True)
        == "verify_tests"
    )


def test_escalation_merge_unverified_for_definitive_no_ci_without_suite():
    # has_ci=False + no detectable suite → merge UNVERIFIED.
    p = _proj({"requires_ci_checks": False, "has_ci": False, "directories": []})
    assert (
        ci_resolution.resolve_no_checks_escalation(p, "main", gate_enabled=True)
        == "merge_unverified"
    )


def test_escalation_merge_unverified_when_kill_switch_off_with_suite():
    # Kill-switch off forces the immediate-pass equivalent even with a suite.
    p = _proj(
        {
            "requires_ci_checks": False,
            "has_ci": False,
            "directories": [{"path": ".", "test_cmd": "pytest"}],
        }
    )
    assert (
        ci_resolution.resolve_no_checks_escalation(p, "main", gate_enabled=False)
        == "merge_unverified"
    )


def test_escalation_merge_unverified_on_internal_staging_base():
    # A staging rollup never runs repo tests → merge_unverified (proceed).
    p = _proj(
        {
            "requires_ci_checks": False,
            "directories": [{"path": ".", "test_cmd": "pytest"}],
        }
    )
    assert (
        ci_resolution.resolve_no_checks_escalation(
            p, "research/cycle-1", gate_enabled=True
        )
        == "merge_unverified"
    )


def test_escalation_keep_polling_for_ci_gated_repo_has_ci_true():
    # has_ci=True (CI-gated) → never auto-merge; keep polling.
    p = _proj({"has_ci": True, "directories": [{"path": ".", "test_cmd": "pytest"}]})
    assert (
        ci_resolution.resolve_no_checks_escalation(p, "main", gate_enabled=True)
        == "keep_polling"
    )


def test_escalation_keep_polling_for_unknown_has_ci_none():
    # has_ci=None (legacy/incomplete detection) → possibly CI-gated; keep polling.
    p = _proj({"has_ci": None, "directories": []})
    assert (
        ci_resolution.resolve_no_checks_escalation(p, "main", gate_enabled=True)
        == "keep_polling"
    )


def test_escalation_keep_polling_for_none_project():
    assert (
        ci_resolution.resolve_no_checks_escalation(None, "main", gate_enabled=True)
        == "keep_polling"
    )


# classify_merge_test_verdict — MERGE_TEST_RESULT artifact → merge-gate outcome


def test_classify_verdict_pass():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": True, "tests": [], "output": ""}
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_heavily_skipped_pass_surfaces_coverage_detail():
    # A detected suite RAN and PASSED but its coverage shows almost every test
    # was skipped (5 passed, 1500 skipped). Outcome stays a non-blocking "pass",
    # but detail surfaces the coverage summary so the operator can tell it apart
    # from a fully-exercised pass.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 5, "skipped": 1500, "total": 1505},
        }
    )
    assert outcome == "pass"
    assert detail == "passed 5, skipped 1500 of 1505"


def test_classify_verdict_pass_low_skip_keeps_empty_detail():
    # A fully-exercised pass (few skips) is unchanged: empty detail.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 1500, "skipped": 5, "total": 1505},
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_pass_below_total_floor_keeps_empty_detail():
    # A tiny suite (total under the floor) where a couple of tests skipped is
    # not flagged — a high ratio on a 5-test suite is meaningless.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 1, "skipped": 4, "total": 5},
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_pass_missing_or_malformed_coverage_is_defensive():
    # Older agents emit no coverage; malformed coverage must not crash. All keep
    # today's ("pass", "") behavior.
    for verdict in (
        {"detected": True, "passed": True},
        {"detected": True, "passed": True, "coverage": None},
        {"detected": True, "passed": True, "coverage": "1500 skipped"},
        {"detected": True, "passed": True, "coverage": {"total": 0}},
        {"detected": True, "passed": True, "coverage": {"skipped": "x", "total": 10}},
    ):
        assert ci_resolution.classify_merge_test_verdict(verdict) == ("pass", "")


def test_classify_verdict_heavily_skipped_pass_still_nonblocking():
    # The enriched detail must not flip the merge gate: a heavily-skipped pass
    # still maps to conclusion="success" with no failed_logs.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 5, "skipped": 1500, "total": 1505},
        }
    )
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"
    assert ci_result["failed_logs"] == ""


def test_classify_verdict_fail_carries_output():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": False, "tests": [], "output": "2 failed"}
    )
    assert outcome == "fail"
    assert detail == "2 failed"


def test_classify_verdict_fail_default_detail_when_output_blank():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": False, "output": ""}
    )
    assert outcome == "fail"
    assert detail  # non-empty fallback message


def test_classify_verdict_detected_false_is_undetected():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": False, "passed": False, "tests": [], "output": "no runner"}
    )
    assert outcome == "undetected"
    assert detail == ""


def test_classify_verdict_partial_setup_failed_blocks_not_falls_open():
    # SOME-but-not-all runners failed setup: a suite never executed. The verdict
    # arrives detected=false (the runners that ran may have passed) but it MUST
    # NOT fall open to undetected — it blocks (fail) so the gate fails closed.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "passed": False,
            "partialSetupFailed": True,
            "output": "Partial setup failure — [npm test (ui)] npm ci failed",
        }
    )
    assert outcome == "fail"
    assert "npm ci failed" in detail


def test_classify_verdict_partial_setup_failed_blank_output_gets_fallback():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": False, "passed": False, "partialSetupFailed": True}
    )
    assert outcome == "fail"
    assert detail  # non-empty fallback naming the partial setup failure


def test_classify_verdict_partial_setup_failed_wins_over_sibling_pass():
    # Even if a sibling runner's pass is reflected as passed=True, the
    # partial-setup signal is checked first and still blocks.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": True, "partialSetupFailed": True}
    )
    assert outcome == "fail"


def test_classify_verdict_none_is_undetected():
    # Verify task crashed / produced no artifact → fall back, don't block.
    assert ci_resolution.classify_merge_test_verdict(None) == ("undetected", "")
    assert ci_resolution.classify_merge_test_verdict("not a dict") == (
        "undetected",
        "",
    )


# disabled_tests_block — defensive parse of the optional VISIBILITY block an
# agent attaches when a PR newly introduces test-skip/disable markers. Parsing
# it NEVER changes the merge decision (visibility-only).


def test_disabled_tests_block_parses_present_block():
    block = ci_resolution.disabled_tests_block(
        {
            "detected": True,
            "passed": True,
            "disabledTests": {
                "count": 2,
                "markers": [
                    {"file": "a.test.ts", "marker": "it.skip", "line": "it.skip('x')"},
                    {
                        "file": "test_b.py",
                        "marker": "@pytest.mark.skip",
                        "line": "@pytest.mark.skip",
                    },
                ],
            },
        }
    )
    assert block is not None
    assert block["count"] == 2
    assert len(block["markers"]) == 2
    assert block["markers"][0] == {
        "file": "a.test.ts",
        "marker": "it.skip",
        "line": "it.skip('x')",
    }


def test_disabled_tests_block_absent_or_malformed_is_none():
    # Missing field, non-dict block, zero/negative/non-int count, and a non-dict
    # verdict all degrade silently to None (older-agent compatible).
    for verdict in (
        {"detected": True, "passed": True},
        {"disabledTests": None},
        {"disabledTests": "2 disabled"},
        {"disabledTests": {"count": 0, "markers": []}},
        {"disabledTests": {"count": -1}},
        {"disabledTests": {"count": "two"}},
        None,
        "not a dict",
    ):
        assert ci_resolution.disabled_tests_block(verdict) is None


def test_disabled_tests_block_caps_markers():
    raw_markers = [
        {"file": f"f{i}.test.ts", "marker": "it.skip", "line": "it.skip()"}
        for i in range(50)
    ]
    block = ci_resolution.disabled_tests_block(
        {"disabledTests": {"count": 50, "markers": raw_markers}}
    )
    assert block is not None
    # The reported count is preserved, but the markers list is bounded.
    assert block["count"] == 50
    assert len(block["markers"]) == 20


def test_classify_verdict_disabled_tests_surfaces_on_pass_without_blocking():
    # A passing suite whose PR newly disabled tests stays a non-blocking "pass",
    # but detail surfaces the count so the operator can see it.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "disabledTests": {
                "count": 3,
                "markers": [
                    {"file": "a.test.ts", "marker": "it.skip", "line": "it.skip()"}
                ],
            },
        }
    )
    assert outcome == "pass"
    assert "disables 3 test(s)" in detail
    # Must not flip the merge gate to a block.
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"
    assert ci_result["failed_logs"] == ""


def test_classify_verdict_disabled_tests_absence_changes_nothing():
    # The SAME verdict with vs without the disabledTests field must produce the
    # identical (outcome, detail) — absence changes nothing.
    base = {"detected": True, "passed": True, "output": ""}
    with_field = {
        **base,
        "disabledTests": {"count": 0, "markers": []},  # malformed/zero → ignored
    }
    assert (
        ci_resolution.classify_merge_test_verdict(base)
        == ci_resolution.classify_merge_test_verdict(with_field)
        == ("pass", "")
    )


def test_classify_verdict_disabled_tests_pairs_with_heavy_skip_blocks():
    # When the PR newly disabled tests AND the resulting "pass" is heavily/mostly
    # skipped, the PR self-suppressed its own suite — this now BLOCKS (fail)
    # rather than passing with a visibility note: the suite was made green by
    # skipping, not by passing.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 5, "skipped": 1500, "total": 1505},
            "disabledTests": {
                "count": 1,
                "markers": [
                    {"file": "a.test.ts", "marker": "it.skip", "line": "it.skip()"}
                ],
            },
        }
    )
    assert outcome == "fail"
    assert "self-suppressed" in detail
    assert "1 test(s)" in detail
    # The detail names the PR-introduced markers so the spawned fix task knows.
    assert "it.skip" in detail
    # And it routes to the block-and-fix path.
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"


# ---------------------------------------------------------------------------
# pr_self_suppressed_suite — the PR's OWN diff disabled tests AND the resulting
# suite no longer verifies anything (all-skipped or mostly-skipped pass). This
# is the new BLOCKING conjunction; the preservation cases keep the conservative
# non-blocking behavior for environmental skips and small flaky quarantines.
# ---------------------------------------------------------------------------
def test_pr_self_suppressed_all_skipped_with_markers_blocks():
    # detected=False + all-skipped + PR newly introduced skip markers => "fail".
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "noTestsReason": "all_skipped",
            "disabledTests": {
                "count": 2,
                "markers": [
                    {"file": "t_a.py", "marker": "@pytest.mark.skip", "line": "1"},
                    {"file": "t_b.py", "marker": "@pytest.mark.skip", "line": "2"},
                ],
            },
        }
    )
    assert outcome == "fail"
    assert "self-suppressed" in detail
    assert "2 test(s)" in detail
    assert "@pytest.mark.skip" in detail
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"


def test_pr_self_suppressed_predicate_conjunction():
    # The helper fires ONLY when BOTH the disabled-tests block AND a non-verifying
    # suite are present.
    disabled = {"count": 1, "markers": []}
    # Both present (all-skipped) => True.
    assert ci_resolution.pr_self_suppressed_suite(
        {"detected": False, "noTestsReason": "all_skipped", "disabledTests": disabled}
    )
    # Both present (heavy-skip pass) => True.
    assert ci_resolution.pr_self_suppressed_suite(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 5, "skipped": 1500, "total": 1505},
            "disabledTests": disabled,
        }
    )
    # Non-verifying but NO disabledTests (environmental skip) => False.
    assert not ci_resolution.pr_self_suppressed_suite(
        {"detected": False, "noTestsReason": "all_skipped"}
    )
    # disabledTests present but suite genuinely runs (fully-exercised pass) => False.
    assert not ci_resolution.pr_self_suppressed_suite(
        {"detected": True, "passed": True, "disabledTests": disabled}
    )


def test_preserve_environmental_all_skipped_without_markers_falls_open():
    # An all-skipped suite where the PR did NOT introduce the skips (no
    # disabledTests block) MUST keep falling open as "undetected".
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": False, "noTestsReason": "all_skipped"}
    )
    assert outcome == "undetected"
    assert detail == ""


def test_preserve_environmental_heavy_skip_pass_without_markers_passes():
    # A mostly-skipped pass with NO disabledTests block keeps passing (visibility
    # detail only).
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 5, "skipped": 1500, "total": 1505},
        }
    )
    assert outcome == "pass"
    assert detail == "passed 5, skipped 1500 of 1505"


def test_preserve_small_flaky_quarantine_with_markers_passes():
    # A small flaky quarantine: a disabledTests block IS present but the suite
    # still genuinely runs (NOT all-skipped, NOT heavy-skip). MUST keep passing
    # with the existing visibility note.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 1400, "skipped": 3, "total": 1403},
            "disabledTests": {
                "count": 3,
                "markers": [{"file": "a.test.ts", "marker": "it.skip", "line": "1"}],
            },
        }
    )
    assert outcome == "pass"
    assert "disables 3 test(s)" in detail


# ---------------------------------------------------------------------------
# build_no_ci_gate_ci_result — outcome → ci_result the orchestrator hands to
# handle_ci_completion. This is the live transition the orchestrator's inline
# no-CI gate performs; pinning it here means a regression that flips a blocking
# `fail` into a merge-allowing `success` fails a test.
# ---------------------------------------------------------------------------
def test_build_ci_result_fail_blocks_merge_with_detail():
    result = ci_resolution.build_no_ci_gate_ci_result("fail", "FAILED test_add")
    assert result["status"] == "completed"
    assert result["conclusion"] == "failure"
    assert result["failed_logs"] == "FAILED test_add"


def test_build_ci_result_fail_blank_detail_gets_fallback_message():
    # A failing verdict must never spawn a fix with empty output.
    result = ci_resolution.build_no_ci_gate_ci_result("fail", "")
    assert result["conclusion"] == "failure"
    assert result["failed_logs"] == "Detected test suite failed."


def test_build_ci_result_pass_proceeds_to_merge():
    result = ci_resolution.build_no_ci_gate_ci_result("pass")
    assert result["status"] == "completed"
    assert result["conclusion"] == "success"
    assert result["failed_logs"] == ""


def test_build_ci_result_undetected_proceeds_unverified():
    # detected=false / no usable verdict → proceed-without-checks (merge UNVERIFIED).
    result = ci_resolution.build_no_ci_gate_ci_result("undetected")
    assert result["conclusion"] == "success"
    assert result["failed_logs"] == ""


# ---------------------------------------------------------------------------
# classify_unverified_fall_open_reason — refine the operator-visible reason a
# detected=false fall-open records on merge_gate_runtime.last_unverified_reason.
# Purely cosmetic to operators: the pass/fail/undetected decision is unchanged.
# ---------------------------------------------------------------------------
def test_fall_open_reason_all_skipped_gets_distinct_key():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {"detected": False, "noTestsReason": "all_skipped"}
    )
    assert key == "suite_all_skipped"
    assert phrase == "suite present but every test skipped"


def test_fall_open_reason_all_skipped_case_insensitive_variants():
    for value in ("ALL_SKIPPED", "all_tests_skipped", "every_test_skipped", "skipped"):
        _, key = ci_resolution.classify_unverified_fall_open_reason(
            {"detected": False, "noTestsReason": value}
        )
        assert key == "suite_all_skipped", value


def test_fall_open_reason_declared_no_artifact_gets_distinct_key():
    # A PASSED declared build probe that confidently observed it wrote NOTHING
    # is a likely no-op build — distinct honest phrase + key, not the confident
    # "deliverable builds" wording.
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "declared": True,
                "label": "make build",
                "wrote_no_artifact": True,
            },
        }
    )
    assert key == "probe_no_artifact"
    assert "no artifact" in phrase
    assert "make build" in phrase


def test_fall_open_reason_declared_no_artifact_without_label():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "declared": True,
                "wrote_no_artifact": True,
            },
        }
    )
    assert key == "probe_no_artifact"
    assert phrase == "declared build ran but emitted no artifact (possible no-op build)"


def test_fall_open_reason_declared_build_passed_without_flag_keeps_probe_passed():
    # Absent flag, false flag, and observation-unavailable (older agent / ambiguous
    # tree) ALL keep today's probe_passed behaviour exactly — the new reason is
    # never emitted on uncertainty.
    for probe in (
        {
            "ran": True,
            "passed": True,
            "could_not_run": False,
            "declared": True,
            "label": "make build",
        },
        {
            "ran": True,
            "passed": True,
            "could_not_run": False,
            "declared": True,
            "label": "make build",
            "wrote_no_artifact": False,
        },
    ):
        phrase, key = ci_resolution.classify_unverified_fall_open_reason(
            {"detected": False, "probe": probe}
        )
        assert key == "probe_passed", probe
        assert phrase == "declared build command passed (make build)", probe


def test_fall_open_reason_no_artifact_flag_ignored_for_non_declared_probe():
    # A GENERIC (non-declared) probe carrying the flag is unaffected — only the
    # declared-build branch honours wrote_no_artifact.
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "declared": False,
                "wrote_no_artifact": True,
            },
        }
    )
    assert key == "probe_passed"
    assert phrase == "acceptance probe passed (changed code compiles/builds)"


def test_fall_open_reason_no_artifact_flag_ignored_for_could_not_run_probe():
    # A could_not_run declared probe carrying the flag still maps to
    # probe_could_not_run — the no-op refinement only applies to a PASSED probe.
    _, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": True,
                "declared": True,
                "label": "make build",
                "wrote_no_artifact": True,
            },
        }
    )
    assert key == "probe_could_not_run"


def test_fall_open_reason_no_suite_keeps_generic_reason():
    # A genuine no-suite repo (no noTestsReason field) keeps today's reason.
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {"detected": False, "passed": False, "output": "no runner"}
    )
    assert key == "undetected_runtime"
    assert phrase == "no test command detectable"


def test_fall_open_reason_unrelated_no_tests_reason_keeps_generic():
    # An older/other-shape value (e.g. a placeholder marker) is not all-skipped.
    _, key = ci_resolution.classify_unverified_fall_open_reason(
        {"detected": False, "noTestsReason": "placeholder"}
    )
    assert key == "undetected_runtime"


def test_fall_open_reason_missing_or_old_shape_verdict_is_defensive():
    # An older agent that never emits the field, or a non-dict, keeps the
    # generic reason — the merge decision is unaffected.
    assert ci_resolution.classify_unverified_fall_open_reason(None) == (
        "no test command detectable",
        "undetected_runtime",
    )
    assert ci_resolution.classify_unverified_fall_open_reason("nope") == (
        "no test command detectable",
        "undetected_runtime",
    )
    assert ci_resolution.classify_unverified_fall_open_reason({}) == (
        "no test command detectable",
        "undetected_runtime",
    )


@pytest.mark.asyncio
async def test_branch_head_has_checks_returns_branch_head_source(mocker):
    """Normal case: branch HEAD carries a completed green build → resolved on the
    branch HEAD itself, no PR-merge-SHA fallback needed."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [{"name": "ci"}],
            }
        ),
    )
    list_prs = mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_abc",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["source"] == "branch_head"
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == "branch_head_abc"
    assert result["conclusion"] == "success"
    # No PR-lookup fallback when the branch HEAD itself resolves to a
    # completed verdict — the resolver short-circuits at step 1.
    list_prs.assert_not_called()


@pytest.mark.asyncio
async def test_branch_head_bare_pr_event_merge_sha_resolves_green(mocker):
    """PR-driven CI shape: branch HEAD has 0 check-runs, but the PR-event merge
    SHA (returned via ``commits/{sha}/pulls``) carries a green build →
    ci_passed=True, source='merge_sha'."""
    pr_head_sha = "pr_event_merge_sha_001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {
            "status": "completed",
            "conclusion": "success",
            "details": [{"name": "ci"}],
        }

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["source"] == "merge_sha"
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == pr_head_sha


@pytest.mark.asyncio
async def test_branch_head_bare_no_pr_event_resolves_no_checks_not_green(mocker):
    """Branch HEAD has 0 check-runs AND no PR-event merge SHA exists →
    ci_passed=False.  This is the bug the strict mode fixes: the lenient
    default would mark this green and the orchestrator would attempt to merge
    against the absence of a CI signal.

    The state is ``no_checks`` (a *definitive* absence: we queried HEAD and
    found no PR-event SHA to fall back to), NOT ``pending`` — so the merge gate
    can distinguish "CI will never run here" (e.g. an internal staging-branch
    PR that CI path-filters out) from a transient resolution failure. It still
    refuses to pass: ci_passed is False on CI-gated bases."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "no_checks", "conclusion": None, "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    assert result["state"] == "no_checks"


@pytest.mark.asyncio
async def test_all_pr_heads_no_checks_returns_no_checks(mocker):
    """Branch HEAD bare and every reachable PR-event head also carries zero
    check-runs → ``no_checks`` (definitive). Distinguishes this from a head
    with in-progress checks (which must stay ``pending``)."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        # Every SHA — branch HEAD and the PR head — has no checks.
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_no_checks"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "no_checks"


@pytest.mark.asyncio
async def test_pr_head_in_progress_returns_pending_not_no_checks(mocker):
    """Branch HEAD bare but a PR-event head has checks IN PROGRESS → ``pending``
    (a real, not-yet-final signal). Must NOT be reported as ``no_checks`` —
    that would let the staging-branch merge bypass fire while CI is still
    running."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "in_progress", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_running"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_pr_head_fetch_error_returns_pending_not_no_checks(mocker):
    """Branch HEAD bare and the only PR-event head's check-runs fetch raises →
    ``pending`` (transient). A fetch error must not be mistaken for a confirmed
    absence of checks, or the staging-branch bypass could merge on a network
    blip."""

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        raise RuntimeError("network down")

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "pr_head_err"},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_branch_head_has_checks_red_returns_red(mocker):
    """Branch HEAD has a completed failing build → ci_passed=False, state=red."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "ci", "conclusion": "failure"}],
            }
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_red",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "branch_head"
    assert result["state"] == "red"


@pytest.mark.asyncio
async def test_branch_head_red_threads_failed_logs(mocker):
    """Regression for CI poll-path spawning blind fix tasks: resolve_pr_ci_status
    must include failed_logs in the return dict when state=red so the orchestrator
    can populate ci_result['failed_logs'] with real detail.

    Before the fix the orchestrator hardcoded failed_logs='' and fix agents had no
    failing-test selectors to run."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "ci-build", "conclusion": "failure"}],
                "failed_logs": "  [ci-build] Failing tests:\n    FAILED testing/test_foo.py::test_bar",
            }
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_red_logs",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["state"] == "red"
    assert result["source"] == "branch_head"
    assert "failed_logs" in result
    assert "test_foo.py::test_bar" in result["failed_logs"]


@pytest.mark.asyncio
async def test_merge_sha_red_threads_failed_logs(mocker):
    """Regression: resolve_pr_ci_status must include failed_logs when state=red
    via the PR-event merge SHA (merge_sha source) as well as the branch_head source."""
    pr_head_sha = "pr_event_red_sha"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_branch_head":
            return {"status": "no_checks", "conclusion": None, "details": []}
        # PR-event SHA: failed with logs
        return {
            "status": "completed",
            "conclusion": "failure",
            "details": [{"name": "backend", "conclusion": "failure"}],
            "failed_logs": "  [backend] Failing tests:\n    FAILED testing/test_bar.py::test_baz",
        }

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": None,
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="bare_branch_head",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["state"] == "red"
    assert result["source"] == "merge_sha"
    assert "failed_logs" in result
    assert "test_bar.py::test_baz" in result["failed_logs"]


@pytest.mark.asyncio
async def test_branch_head_in_progress_returns_pending(mocker):
    """Branch HEAD has in-progress checks → ci_passed=False, state=pending.
    Stays on branch_head source — we have a real signal, just not final yet."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "in_progress",
                "conclusion": None,
                "details": [],
            }
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_pending",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "branch_head"
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_branch_head_fetch_failure_returns_unresolved(mocker):
    """Any exception from get_check_runs_cached at step 1 → unresolved (never
    raises into the orchestrator's cycle loop)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=RuntimeError("network down")),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="branch_head_fail",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_empty_branch_sha_returns_unresolved(mocker):
    """Empty branch_sha short-circuits to unresolved without any API call."""
    cr = mocker.patch(
        "app.github.get_check_runs_cached", new=AsyncMock(return_value=None)
    )
    list_prs = mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=42,
        branch_sha="",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["source"] == "unresolved"
    cr.assert_not_called()
    list_prs.assert_not_called()


@pytest.mark.asyncio
async def test_pr_head_red_beats_green_across_multiple_heads(mocker):
    """Branch HEAD bare; multiple associated PR heads with mixed verdicts —
    a red on any PR head must win over a green on another head regardless of
    the order GitHub returns them."""
    green_sha = "pr_head_green01"
    red_sha = "pr_head_red0001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_multi":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == green_sha:
            return {
                "status": "completed",
                "conclusion": "success",
                "details": [],
            }
        if sha == red_sha:
            return {
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            }
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    # Green head listed FIRST — the resolver must not short-circuit on it.
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 10,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": green_sha},
                },
                {
                    "number": 11,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": red_sha},
                },
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=None,
        branch_sha="bare_multi",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is False
    assert result["state"] == "red"
    assert result["source"] == "merge_sha"
    assert result["head_sha_resolved"] == red_sha


@pytest.mark.asyncio
async def test_explicit_pr_number_filters_associated_prs(mocker):
    """When pr_number is supplied, the resolver prefers that PR's head over
    other associated PRs — supports the orchestrator passing the parking
    task's PR number so we resolve through the PR actually being merged."""
    target_sha = "target_pr_head_aa"
    other_sha = "other_pr_head_bb"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "bare_explicit":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == target_sha:
            return {
                "status": "completed",
                "conclusion": "success",
                "details": [],
            }
        if sha == other_sha:
            return {
                "status": "completed",
                "conclusion": "failure",
                "details": [],
            }
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                # Unrelated PR red — would normally win the red-preferring
                # sweep, but the explicit pr_number=99 filter excludes it.
                {
                    "number": 100,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": other_sha},
                },
                {
                    "number": 99,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": target_sha},
                },
            ]
        ),
    )

    result = await ci_resolution.resolve_pr_ci_status(
        pr_number=99,
        branch_sha="bare_explicit",
        token="tok",
        owner="o",
        repo="r",
    )

    assert result["ci_passed"] is True
    assert result["state"] == "green"
    assert result["head_sha_resolved"] == target_sha


# requires_ci_checks PERSISTENCE — the field must survive the validated write
# path (BuildConfig(**...).model_dump(exclude_none=True)) and the operator-
# override carry-forward used by manual re-detect / empty-config auto-refresh.
# These exercise the actual persistence layer, not a hand-built reader dict.


def test_build_config_schema_persists_requires_ci_checks_false():
    from app.schemas import BuildConfig as BuildConfigSchema

    persisted = BuildConfigSchema(**{"requires_ci_checks": False}).model_dump(
        exclude_none=True
    )
    # exclude_none must NOT strip an explicit False (False is not None).
    assert persisted["requires_ci_checks"] is False


def test_build_config_schema_defaults_requires_ci_checks_true():
    from app.schemas import BuildConfig as BuildConfigSchema

    persisted = BuildConfigSchema().model_dump(exclude_none=True)
    # Default True preserves CI-gated behavior for existing projects.
    assert persisted["requires_ci_checks"] is True


def test_override_merge_preserves_existing_requires_ci_checks_false():
    from app.build_config_detection import apply_operator_build_config_overrides

    # Repo detection never emits requires_ci_checks; a stored False must carry
    # forward onto freshly-detected config so re-detect does not re-gate the repo.
    detected = {"language": "python", "directories": []}
    merged = apply_operator_build_config_overrides(
        detected, {"requires_ci_checks": False}
    )
    assert merged["requires_ci_checks"] is False


def test_override_merge_absent_requires_ci_checks_leaves_detected_unchanged():
    from app.build_config_detection import apply_operator_build_config_overrides

    detected = {"language": "python"}
    merged = apply_operator_build_config_overrides(detected, {"other": 1})
    assert "requires_ci_checks" not in merged


def test_build_config_schema_defaults_promotion_ci_selfheal_true():
    from app.schemas import BuildConfig as BuildConfigSchema

    persisted = BuildConfigSchema().model_dump(exclude_none=True)
    # Default ON for every project: the engine self-heals its own promotion CI.
    assert persisted["promotion_ci_selfheal_enabled"] is True


def test_build_config_schema_persists_promotion_ci_selfheal_false():
    from app.schemas import BuildConfig as BuildConfigSchema

    persisted = BuildConfigSchema(
        **{"promotion_ci_selfheal_enabled": False}
    ).model_dump(exclude_none=True)
    # exclude_none must NOT strip an explicit operator opt-out (False is not None).
    assert persisted["promotion_ci_selfheal_enabled"] is False


def test_override_merge_preserves_existing_promotion_ci_selfheal_false():
    from app.build_config_detection import apply_operator_build_config_overrides

    # Repo detection never emits promotion_ci_selfheal_enabled; a stored operator
    # opt-out must carry forward onto freshly-detected config so re-detect does
    # not silently revert it back to the default-ON.
    detected = {"language": "python", "directories": []}
    merged = apply_operator_build_config_overrides(
        detected, {"promotion_ci_selfheal_enabled": False}
    )
    assert merged["promotion_ci_selfheal_enabled"] is False


def test_override_merge_preserves_existing_requires_ci_checks_true():
    from app.build_config_detection import apply_operator_build_config_overrides

    detected = {"language": "python"}
    merged = apply_operator_build_config_overrides(
        detected, {"requires_ci_checks": True}
    )
    assert merged["requires_ci_checks"] is True


def test_override_merge_preserves_parallelism_caps():
    from app.build_config_detection import apply_operator_build_config_overrides

    # Repo detection never emits the per-project parallelism caps; stored
    # values must carry forward so re-detect does not silently lift a throttle.
    detected = {"language": "python", "directories": []}
    merged = apply_operator_build_config_overrides(
        detected,
        {
            "max_parallel_research": 2,
            "max_total_research": 4,
            "max_autonomous_tasks_per_hour": 8,
        },
    )
    assert merged["max_parallel_research"] == 2
    assert merged["max_total_research"] == 4
    assert merged["max_autonomous_tasks_per_hour"] == 8


# ── Acceptance probe: classify_merge_test_verdict (detected=false fall-open) ──
def test_classify_verdict_probe_fail_blocks_with_output():
    # A clear acceptance-probe failure on a no-suite fall-open flips the
    # detected=false verdict to a merge-blocking "fail" carrying the probe output.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "noTestsReason": "no_suite",
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "ecosystem": "python",
                "output": "SyntaxError: invalid syntax",
            },
        }
    )
    assert outcome == "fail"
    assert "SyntaxError" in detail


def test_classify_verdict_probe_fail_blank_output_gets_fallback():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "output": "",
            },
        }
    )
    assert outcome == "fail"
    assert detail  # non-empty fallback message


def test_classify_verdict_probe_could_not_run_stays_undetected():
    # Missing deps / unprovisioned setup must NOT hard-block — fall open.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": True,
                "output": "",
            },
        }
    )
    assert outcome == "undetected"
    assert detail == ""


def test_classify_verdict_probe_pass_stays_undetected():
    # A passing probe is a positive signal but still no unit suite — fall open
    # (UNVERIFIED), never a hard "pass" of the test gate.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "output": "",
            },
        }
    )
    assert outcome == "undetected"


def test_classify_verdict_probe_not_ran_stays_undetected():
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {"ran": False, "passed": False, "could_not_run": False},
        }
    )
    assert outcome == "undetected"


def test_classify_verdict_no_probe_key_unchanged_undetected():
    # Older agent emits no probe — behavior byte-for-byte unchanged.
    assert ci_resolution.classify_merge_test_verdict(
        {"detected": False, "passed": False, "output": "no runner"}
    ) == ("undetected", "")


def test_classify_verdict_detected_true_ignores_generic_probe():
    # A real suite verdict is never reinterpreted by a GENERIC (non-declared)
    # probe field — the generic compile probe stays detected=false-only.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "probe": {"ran": True, "passed": False, "could_not_run": False},
        }
    )
    assert outcome == "pass"


# ── Declared build probe BLOCKS regardless of suite detection ─────────────────
# A green unit suite must NOT mask a broken DECLARED build (bundled frontends,
# tests that exercise source without the build step). The declared-build clear
# failure is HIGH precedence and fires even on detected=true + passed.
def test_classify_verdict_declared_build_fail_blocks_passing_suite():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "declared": True,
                "ecosystem": "node",
                "output": "webpack: build error in module foo",
                "label": "npm run build (admin-ui)",
            },
        }
    )
    assert outcome == "fail"
    assert "Declared build command `npm run build (admin-ui)` failed" in detail
    assert "webpack: build error" in detail
    # Routes to the block-and-fix path carrying the declared-build output.
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"


def test_classify_verdict_declared_build_pass_on_passing_suite_stays_pass():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "declared": True,
                "label": "npm run build (admin-ui)",
            },
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_declared_build_could_not_run_on_passing_suite_stays_pass():
    # A declared build that could not run (missing deps / unprovisioned setup)
    # must NOT block a passing suite — conservative fall-open.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": True,
                "declared": True,
                "label": "npm run build (admin-ui)",
            },
        }
    )
    assert outcome == "pass"


def test_declared_build_is_clear_failure_predicate():
    # Fires only for a declared probe that ran, did not pass, and was not a
    # could-not-run degrade. A generic / passing / could-not-run / missing probe
    # returns False.
    assert ci_resolution.declared_build_is_clear_failure(
        {"ran": True, "passed": False, "could_not_run": False, "declared": True}
    )
    assert not ci_resolution.declared_build_is_clear_failure(
        {"ran": True, "passed": False, "could_not_run": False}  # not declared
    )
    assert not ci_resolution.declared_build_is_clear_failure(
        {"ran": True, "passed": True, "could_not_run": False, "declared": True}
    )
    assert not ci_resolution.declared_build_is_clear_failure(
        {"ran": True, "passed": False, "could_not_run": True, "declared": True}
    )
    assert not ci_resolution.declared_build_is_clear_failure(None)


# ── Acceptance probe: classify_unverified_fall_open_reason ────────────────────
def test_fall_open_reason_probe_passed_distinct_key():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {"ran": True, "passed": True, "could_not_run": False},
        }
    )
    assert key == "probe_passed"
    assert "probe" in phrase


def test_fall_open_reason_probe_could_not_run_distinct_key():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {"ran": True, "passed": False, "could_not_run": True},
        }
    )
    assert key == "probe_could_not_run"
    assert "probe" in phrase


def test_fall_open_reason_probe_takes_precedence_over_all_skipped():
    _, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "noTestsReason": "all_skipped",
            "probe": {"ran": True, "passed": True, "could_not_run": False},
        }
    )
    assert key == "probe_passed"


def test_fall_open_reason_no_probe_keeps_all_skipped():
    # Without a probe the all-skipped reason is unchanged.
    _, key = ci_resolution.classify_unverified_fall_open_reason(
        {"detected": False, "noTestsReason": "all_skipped"}
    )
    assert key == "suite_all_skipped"


# ── Declared build_cmd probe: operator-visible wording (key unchanged) ────────
def test_fall_open_reason_declared_build_passed_wording():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": True,
                "could_not_run": False,
                "declared": True,
                "label": "npm run build (admin-ui)",
            },
        }
    )
    # Key stays backward-compatible; only the human phrase distinguishes declared.
    assert key == "probe_passed"
    assert "declared build command passed" in phrase
    assert "npm run build (admin-ui)" in phrase


def test_fall_open_reason_declared_build_could_not_run_wording():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": True,
                "declared": True,
                "label": "make build (root)",
            },
        }
    )
    assert key == "probe_could_not_run"
    assert "declared build command could not run" in phrase
    assert "make build (root)" in phrase


def test_fall_open_reason_generic_probe_unchanged_when_not_declared():
    # A generic probe (no declared marker) keeps the original phrasing.
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "probe": {"ran": True, "passed": True, "could_not_run": False},
        }
    )
    assert key == "probe_passed"
    assert phrase == "acceptance probe passed (changed code compiles/builds)"


def test_classify_verdict_declared_build_fail_carries_declared_detail():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "declared": True,
                "label": "npm run build (admin-ui)",
                "output": "webpack: build error in module foo",
            },
        }
    )
    assert outcome == "fail"
    assert "Declared build command `npm run build (admin-ui)` failed" in detail
    assert "webpack: build error in module foo" in detail


def test_classify_verdict_declared_build_fail_blank_output_fallback():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "declared": True,
                "label": "make (root)",
                "output": "",
            },
        }
    )
    assert outcome == "fail"
    assert "Declared build command `make (root)` failed" in detail


# ---------------------------------------------------------------------------
# record_no_ci_gate_runtime / merge_gate_runtime_block — rolling history,
# per-reason tally, and the derived at-a-glance summary. These are pure
# in-memory operations on project.merge_gate_runtime (no DB needed): the
# recorder is best-effort and stages the project; the block normalizer reads
# the in-memory column back.
# ---------------------------------------------------------------------------


def _record(project, *, verified, reason, when):
    ci_resolution.record_no_ci_gate_runtime(
        None,
        project,
        executed_real_tests=verified,
        unverified_reason=reason,
        when=when,
    )


def test_runtime_history_keeps_four_legacy_keys_and_adds_outcome():
    project = _proj()
    _record(
        project, verified=False, reason="undetected_runtime", when="2026-06-01T00:00:00"
    )
    block = ci_resolution.merge_gate_runtime_block(project)
    # Legacy keys unchanged.
    assert block["last_run_at"] == "2026-06-01T00:00:00"
    assert block["executed_real_tests"] is False
    assert block["last_unverified_at"] == "2026-06-01T00:00:00"
    assert block["last_unverified_reason"] == "undetected_runtime"
    # New rolling history present with one entry.
    assert block["recent_outcomes"] == [
        {"at": "2026-06-01T00:00:00", "verified": False, "reason": "undetected_runtime"}
    ]
    assert block["unverified_reason_counts"] == {"undetected_runtime": 1}


def test_runtime_verified_entry_has_no_reason_and_no_tally():
    project = _proj()
    _record(project, verified=True, reason=None, when="2026-06-02T00:00:00")
    block = ci_resolution.merge_gate_runtime_block(project)
    assert block["recent_outcomes"] == [
        {"at": "2026-06-02T00:00:00", "verified": True, "reason": None}
    ]
    # A verified run never creates the tally.
    assert "unverified_reason_counts" not in block
    assert block["summary"]["verified_runs"] == 1
    assert block["summary"]["fell_open_runs"] == 0
    assert block["summary"]["total_fall_opens"] == 0
    assert block["summary"]["most_common_reason"] is None


def test_runtime_history_bounded_at_cap_drops_oldest():
    project = _proj()
    # Record well past the cap; oldest must be evicted, newest retained.
    total = ci_resolution._RECENT_OUTCOMES_CAP + 5
    for i in range(total):
        _record(project, verified=True, reason=None, when=f"2026-06-05T00:00:{i:02d}")
    block = ci_resolution.merge_gate_runtime_block(project)
    outcomes = block["recent_outcomes"]
    assert len(outcomes) == ci_resolution._RECENT_OUTCOMES_CAP
    # Oldest dropped, newest kept (most recent last).
    assert (
        outcomes[0]["at"]
        == f"2026-06-05T00:00:{(total - ci_resolution._RECENT_OUTCOMES_CAP):02d}"
    )
    assert outcomes[-1]["at"] == f"2026-06-05T00:00:{(total - 1):02d}"


def test_runtime_tally_accumulates_per_reason_across_repeated_fall_opens():
    project = _proj()
    _record(
        project, verified=False, reason="undetected_runtime", when="2026-06-01T00:00:01"
    )
    _record(
        project, verified=False, reason="undetected_runtime", when="2026-06-01T00:00:02"
    )
    _record(
        project, verified=False, reason="suite_all_skipped", when="2026-06-01T00:00:03"
    )
    _record(project, verified=True, reason=None, when="2026-06-01T00:00:04")
    block = ci_resolution.merge_gate_runtime_block(project)
    assert block["unverified_reason_counts"] == {
        "undetected_runtime": 2,
        "suite_all_skipped": 1,
    }
    summary = block["summary"]
    assert summary["window"] == 4
    assert summary["verified_runs"] == 1
    assert summary["fell_open_runs"] == 3
    assert summary["total_fall_opens"] == 3
    # Most-tallied reason wins the at-a-glance line.
    assert summary["most_common_reason"] == "undetected_runtime"


def test_runtime_total_fall_opens_can_exceed_window_after_eviction():
    project = _proj()
    # First fall-open of a rare reason, then flood the window with verified runs
    # so the rare fall-open is evicted from recent_outcomes but stays tallied.
    _record(
        project, verified=False, reason="suite_all_skipped", when="2026-06-05T00:00:00"
    )
    for i in range(1, ci_resolution._RECENT_OUTCOMES_CAP + 1):
        _record(project, verified=True, reason=None, when=f"2026-06-05T00:01:{i:02d}")
    block = ci_resolution.merge_gate_runtime_block(project)
    # Evicted from the window...
    assert all(o["reason"] != "suite_all_skipped" for o in block["recent_outcomes"])
    assert block["summary"]["fell_open_runs"] == 0
    # ...but the all-time tally still remembers it.
    assert block["unverified_reason_counts"] == {"suite_all_skipped": 1}
    assert block["summary"]["total_fall_opens"] == 1
    assert block["summary"]["most_common_reason"] == "suite_all_skipped"


def test_runtime_block_none_when_never_run():
    project = _proj()
    assert project.merge_gate_runtime is None
    assert ci_resolution.merge_gate_runtime_block(project) is None


def test_runtime_block_legacy_four_key_record_omits_new_fields():
    """A record written by the old code (four keys only) normalizes without the
    new history fields — backward compatible."""
    project = _proj()
    project.merge_gate_runtime = {
        "last_run_at": "2026-06-01T00:00:00",
        "executed_real_tests": False,
        "last_unverified_at": "2026-06-01T00:00:00",
        "last_unverified_reason": "undetected_runtime",
    }
    block = ci_resolution.merge_gate_runtime_block(project)
    assert block["last_unverified_reason"] == "undetected_runtime"
    assert "recent_outcomes" not in block
    assert "unverified_reason_counts" not in block
    assert "summary" not in block


def test_runtime_block_malformed_history_is_defensive():
    """Non-list recent_outcomes / non-dict tally are ignored, not raised on."""
    project = _proj()
    project.merge_gate_runtime = {
        "last_run_at": "2026-06-01T00:00:00",
        "executed_real_tests": True,
        "recent_outcomes": "not-a-list",
        "unverified_reason_counts": ["not", "a", "dict"],
    }
    block = ci_resolution.merge_gate_runtime_block(project)
    assert "recent_outcomes" not in block
    assert "unverified_reason_counts" not in block
    assert "summary" not in block


# --- benign-vs-degraded UNVERIFIED-merge reason partition -------------------


def test_unverified_reason_sets_are_disjoint():
    """The benign and degraded reason sets must not overlap — a key is one or
    the other, never both."""
    overlap = (
        ci_resolution.BENIGN_UNVERIFIED_REASONS
        & ci_resolution.DEGRADED_UNVERIFIED_REASONS
    )
    assert not overlap, (
        f"reason keys classified as BOTH benign and degraded: {sorted(overlap)}"
    )


def test_unverified_reason_sets_union_is_canonical():
    """Their union must equal the canonical emitted reason set."""
    canonical = {
        "no_detectable_test_command",
        "kill_switch_off",
        "internal_staging_base",
        "undetected_runtime",
        "suite_all_skipped",
        "verify_setup_failed",
        "probe_passed",
        "probe_could_not_run",
        "probe_no_artifact",
        "documented_command_could_not_run",
        "documented_command_passed",
        "documented_command_no_artifact",
        "acceptance_test_not_run",
    }
    union = (
        ci_resolution.BENIGN_UNVERIFIED_REASONS
        | ci_resolution.DEGRADED_UNVERIFIED_REASONS
    )
    assert union == canonical


def test_is_degraded_unverified_reason_true_for_degraded():
    for reason in ci_resolution.DEGRADED_UNVERIFIED_REASONS:
        assert ci_resolution.is_degraded_unverified_reason(reason) is True, reason


def test_is_degraded_unverified_reason_false_for_benign_and_unknown():
    for reason in ci_resolution.BENIGN_UNVERIFIED_REASONS:
        assert ci_resolution.is_degraded_unverified_reason(reason) is False, reason
    assert ci_resolution.is_degraded_unverified_reason("totally_unknown") is False
    assert ci_resolution.is_degraded_unverified_reason(None) is False


def test_alert_severity_map_derives_from_shared_sets():
    """The alert-layer severity map must agree with the shared partition byte-for-
    byte: every degraded reason -> ERROR, every benign reason -> WARNING, and the
    map covers exactly the canonical union (single-source-of-truth wiring)."""
    from app.alert_dispatch import _UNVERIFIED_REASON_SEVERITY, AlertSeverity

    expected = {
        **{r: AlertSeverity.ERROR for r in ci_resolution.DEGRADED_UNVERIFIED_REASONS},
        **{r: AlertSeverity.WARNING for r in ci_resolution.BENIGN_UNVERIFIED_REASONS},
    }
    assert _UNVERIFIED_REASON_SEVERITY == expected


# ── Declared acceptance-test gate ─────────────────────────────────────────────
def test_acceptance_test_block_parses_well_formed():
    block = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "descriptor": "the seam test",
                "selector": "testing/test_seam.py::test_x",
                "present": True,
                "ran": True,
                "status": "ran",
            }
        }
    )
    assert block is not None
    assert block["status"] == "ran"
    assert block["ran"] is True
    assert block["selector"] == "testing/test_seam.py::test_x"


def test_acceptance_test_block_defensive_none():
    # Missing / non-dict / not-declared / bad-status all degrade to None.
    assert ci_resolution.acceptance_test_block(None) is None
    assert ci_resolution.acceptance_test_block({}) is None
    assert ci_resolution.acceptance_test_block({"acceptanceTest": "x"}) is None
    assert (
        ci_resolution.acceptance_test_block(
            {"acceptanceTest": {"declared": False, "status": "ran"}}
        )
        is None
    )
    assert (
        ci_resolution.acceptance_test_block(
            {"acceptanceTest": {"declared": True, "status": "weird"}}
        )
        is None
    )


def test_acceptance_test_is_clear_failure_predicate():
    # Absent or skipped (and not ran) is a clear failure; ran / could_not_run /
    # non-declared / missing are not.
    assert ci_resolution.acceptance_test_is_clear_failure(
        {"declared": True, "ran": False, "status": "absent"}
    )
    assert ci_resolution.acceptance_test_is_clear_failure(
        {"declared": True, "ran": False, "status": "skipped"}
    )
    assert not ci_resolution.acceptance_test_is_clear_failure(
        {"declared": True, "ran": True, "status": "ran"}
    )
    assert not ci_resolution.acceptance_test_is_clear_failure(
        {"declared": True, "ran": False, "status": "could_not_run"}
    )
    assert not ci_resolution.acceptance_test_is_clear_failure(
        {"declared": False, "ran": False, "status": "absent"}
    )
    assert not ci_resolution.acceptance_test_is_clear_failure(None)


def test_acceptance_test_block_parses_passed_defensively():
    # ``passed`` is parsed as a defensive bool: only a literal True yields True.
    green = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "descriptor": "seam test",
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": True,
            }
        }
    )
    assert green is not None and green["passed"] is True
    # A missing ``passed`` field (older agent) defaults to False — no green signal.
    older = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
            }
        }
    )
    assert older is not None and older["passed"] is False
    # A truthy-but-non-bool value ("true") is NOT green.
    malformed = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": "true",
            }
        }
    )
    assert malformed is not None and malformed["passed"] is False


def test_acceptance_test_is_green_predicate():
    # Green ONLY when declared + ran + status=="ran" + passed is True.
    assert ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": True}
    )
    # Ran but the targeted run failed → not green.
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": False}
    )
    # Skipped / absent are never green (and status must be "ran").
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": False, "status": "skipped", "passed": True}
    )
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": False, "status": "absent", "passed": True}
    )
    # Missing ``passed`` (older agent) → not green.
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran"}
    )
    # Non-declared / non-dict / None → not green.
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": False, "ran": True, "status": "ran", "passed": True}
    )
    assert not ci_resolution.acceptance_test_is_green(None)
    assert not ci_resolution.acceptance_test_is_green("x")


def test_acceptance_test_block_carries_in_diff():
    # ``in_diff`` is surfaced ONLY when the agent emitted a literal bool.
    in_diff_true = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": True,
                "in_diff": True,
            }
        }
    )
    assert in_diff_true is not None and in_diff_true["in_diff"] is True
    in_diff_false = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": True,
                "in_diff": False,
            }
        }
    )
    assert in_diff_false is not None and in_diff_false["in_diff"] is False
    # An older agent that omits ``in_diff`` leaves the key absent (not False),
    # so today's behavior is preserved.
    older = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": True,
            }
        }
    )
    assert older is not None and "in_diff" not in older
    # A non-bool ``in_diff`` (malformed) is ignored — key absent.
    malformed = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "present": True,
                "ran": True,
                "status": "ran",
                "passed": True,
                "in_diff": "true",
            }
        }
    )
    assert malformed is not None and "in_diff" not in malformed


def test_acceptance_test_not_in_diff_is_still_green():
    # A declared test that ran green but is NOT among the PR's changed files
    # (in_diff=False) is the behaviour-preserving-refactor shape: the acceptance
    # gate credits it via existence-at-merge, so the targeted-green mint MUST fire.
    # Withholding it here is what previously made that whole path dead.
    assert ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": True, "in_diff": False}
    )
    # A test that did not actually pass is still not green, in_diff notwithstanding.
    assert not ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": False, "in_diff": False}
    )
    # in_diff=True keeps the green mint.
    assert ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": True, "in_diff": True}
    )
    # Omitted in_diff (older agent) preserves today's green mint.
    assert ci_resolution.acceptance_test_is_green(
        {"declared": True, "ran": True, "status": "ran", "passed": True}
    )


def test_acceptance_test_not_in_diff_end_to_end_mints_green():
    # A full verdict whose acceptanceTest ran green but reports in_diff=False
    # flows through acceptance_test_block → acceptance_test_is_green as GREEN, so
    # the no-CI monitor stamps the targeted-green signal the acceptance gate reads.
    verdict = {
        "detected": False,
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
    block = ci_resolution.acceptance_test_block(verdict)
    # in_diff is still PARSED and surfaced — it is informational, not a gate.
    assert block is not None and block["in_diff"] is False
    assert ci_resolution.acceptance_test_is_green(block) is True
    # It is also NOT a clear failure — the test DID run.
    assert ci_resolution.acceptance_test_is_clear_failure(block) is False


def test_acceptance_test_is_green_end_to_end_from_verdict():
    # A full verify-tests verdict whose acceptanceTest block ran green flows
    # through acceptance_test_block → acceptance_test_is_green.
    verdict = {
        "detected": False,
        "acceptanceTest": {
            "declared": True,
            "descriptor": "the seam test",
            "selector": "testing/test_seam.py::test_x",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    }
    block = ci_resolution.acceptance_test_block(verdict)
    assert ci_resolution.acceptance_test_is_green(block) is True
    # And it is NOT a clear failure (that path is for absent/skipped).
    assert ci_resolution.acceptance_test_is_clear_failure(block) is False


def test_classify_verdict_absent_acceptance_test_blocks_passing_suite():
    # A green unit suite must NOT let a promised-but-missing test merge as proven.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
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
        }
    )
    assert outcome == "fail"
    assert "acceptance test" in detail.lower()
    assert "testing/test_seam.py::test_x" in detail
    assert "absent" in detail.lower()
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"


def test_classify_verdict_skipped_acceptance_test_blocks_passing_suite():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "the seam test",
                "selector": "testing/test_seam.py::test_x",
                "present": True,
                "ran": False,
                "status": "skipped",
            },
        }
    )
    assert outcome == "fail"
    assert "skipped" in detail.lower()


def test_classify_verdict_ran_acceptance_test_keeps_pass():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
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
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_acceptance_test_block_parses_in_diff_field():
    # Only a literal True/False is meaningful; anything else omits the key.
    in_diff_true = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "descriptor": "x",
                "status": "ran",
                "ran": True,
                "in_diff": True,
            }
        }
    )
    assert in_diff_true["in_diff"] is True
    in_diff_false = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "descriptor": "x",
                "status": "ran",
                "ran": True,
                "in_diff": False,
            }
        }
    )
    assert in_diff_false["in_diff"] is False
    # Older agent (no in_diff) / non-bool value → key absent (indeterminate).
    absent = ci_resolution.acceptance_test_block(
        {"acceptanceTest": {"declared": True, "descriptor": "x", "status": "ran"}}
    )
    assert "in_diff" not in absent
    non_bool = ci_resolution.acceptance_test_block(
        {
            "acceptanceTest": {
                "declared": True,
                "descriptor": "x",
                "status": "ran",
                "in_diff": "yes",
            }
        }
    )
    assert "in_diff" not in non_bool


def test_acceptance_test_not_in_diff_predicate_is_retired():
    # The not-in-diff CLEAR-FAILURE predicate is GONE, together with the pre-merge
    # block it fed. Its rationale ("the post-merge gate mints proven=False anyway")
    # stopped being true once _evaluate_named_test began crediting
    # existence-at-merge, so keeping it would block the very shape the gate credits.
    assert not hasattr(ci_resolution, "acceptance_test_not_in_diff_is_clear_failure")


def test_classify_verdict_ran_green_test_not_in_diff_keeps_pass():
    # A declared test that ran GREEN but lives outside the PR's own diff is the
    # behaviour-preserving-refactor shape. It must NOT be blocked pre-merge —
    # blocking here strands the merge before the acceptance gate can credit it.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
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
    )
    assert outcome == "pass"
    assert detail == ""
    ci_result = ci_resolution.build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"
    # An ABSENT or SKIPPED declared test is still a hard block — removing the
    # not-in-diff branch must not weaken the absent/skipped guard next to it.
    absent_outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "the seam test",
                "present": False,
                "ran": False,
                "status": "absent",
                "in_diff": False,
            },
        }
    )
    assert absent_outcome == "fail"


def test_classify_verdict_ran_green_test_in_diff_keeps_pass():
    # The co-modified case produces NO block signal.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
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
                "in_diff": True,
            },
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_ran_test_indiff_absent_keeps_pass():
    # An older agent (or diff-unavailable run) that omits in_diff keeps today's
    # non-blocking pass — the signal never fires on absence.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
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
            },
        }
    )
    assert outcome == "pass"
    assert detail == ""


def test_classify_verdict_could_not_run_acceptance_test_does_not_block():
    # A check that could not run (no targetable suite) must NOT hard-block; it
    # degrades to a fall-open instead.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "x",
                "present": False,
                "ran": False,
                "status": "could_not_run",
            },
        }
    )
    assert outcome == "pass"


def test_classify_verdict_no_acceptance_block_unchanged():
    # Absent declaration → today's behavior byte-for-byte.
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": True}
    )
    assert outcome == "pass"
    assert detail == ""


def test_fall_open_reason_acceptance_test_could_not_run_key():
    phrase, key = ci_resolution.classify_unverified_fall_open_reason(
        {
            "detected": False,
            "acceptanceTest": {
                "declared": True,
                "descriptor": "x",
                "selector": "testing/test_seam.py::test_x",
                "present": False,
                "ran": False,
                "status": "could_not_run",
            },
        }
    )
    assert key == "acceptance_test_not_run"
    assert "testing/test_seam.py::test_x" in phrase


def test_acceptance_test_not_run_is_degraded_reason():
    assert ci_resolution.is_degraded_unverified_reason("acceptance_test_not_run")


# ---------------------------------------------------------------------------
# re-poll idempotency guard — the orchestrator's no_ci_runtime_recorded sticky
# marker must prevent record_no_ci_gate_runtime from appending a second entry
# to recent_outcomes or incrementing unverified_reason_counts when a
# still-EVALUATING task is re-polled before it reaches a terminal state.
# ---------------------------------------------------------------------------


def test_repoll_without_guard_inflates_counts():
    """Calling the recorder TWICE without a guard doubles recent_outcomes and
    the per-reason count — this confirms the bug the sticky marker fixes."""
    project = _proj()
    _record(project, verified=False, reason="undetected_runtime", when="2026-06-10T00:00:01")
    _record(project, verified=False, reason="undetected_runtime", when="2026-06-10T00:00:02")
    block = ci_resolution.merge_gate_runtime_block(project)
    # Without a guard, a second poll appends a second entry — the bug.
    assert len(block["recent_outcomes"]) == 2
    assert block["unverified_reason_counts"]["undetected_runtime"] == 2


def test_repoll_with_guard_records_exactly_once():
    """The orchestrator's no_ci_runtime_recorded sticky marker prevents the
    recorder from being called on the second poll of the same EVALUATING task,
    keeping recent_outcomes and the per-reason counter at exactly 1 entry."""
    project = _proj()
    ci_loop: dict = {}

    def _guarded_record(when):
        if not ci_loop.get("no_ci_runtime_recorded"):
            ci_resolution.record_no_ci_gate_runtime(
                None,
                project,
                executed_real_tests=False,
                unverified_reason="undetected_runtime",
                when=when,
            )
            ci_loop["no_ci_runtime_recorded"] = True

    # First poll — records normally.
    _guarded_record("2026-06-10T00:00:01")
    # Second poll — guard fires; recorder is NOT called again.
    _guarded_record("2026-06-10T00:00:02")

    block = ci_resolution.merge_gate_runtime_block(project)
    assert len(block["recent_outcomes"]) == 1
    assert block["recent_outcomes"][0]["at"] == "2026-06-10T00:00:01"
    assert block["unverified_reason_counts"]["undetected_runtime"] == 1


# ---------------------------------------------------------------------------
# Unverifiable baseline attribution must FAIL CLOSED (block), never fall open.
# The agent posts detected=false + verificationUnverified when it could not tell
# which failures belong to this PR (base checkout failed / no structured failure
# report / canonical comparator unavailable). That condition is DETERMINISTIC —
# every verify re-spawn reproduces it — so an "undetected" fall-open would merge
# UNVERIFIED every single time once the crash retries are exhausted.
# ---------------------------------------------------------------------------


def test_classify_verdict_verification_unverified_blocks_not_falls_open():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {
            "detected": False,
            "passed": False,
            "verificationUnverified": True,
            "tests": [],
            "output": "Canonical structured baseline comparator was unavailable.",
        }
    )
    assert outcome == "fail"
    assert "comparator was unavailable" in detail


def test_classify_verdict_verification_unverified_blank_output_gets_fallback():
    outcome, detail = ci_resolution.classify_merge_test_verdict(
        {"detected": False, "passed": False, "verificationUnverified": True}
    )
    assert outcome == "fail"
    assert detail  # non-empty fallback naming the unverified attribution


def test_classify_verdict_verification_unverified_wins_over_passed():
    # A runner that "passed" cannot mask an unattributable baseline.
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        {"detected": True, "passed": True, "verificationUnverified": True}
    )
    assert outcome == "fail"


def test_failed_verify_artifact_forces_verdict_on_unverified_attribution():
    # The orchestrator only reads a verdict from a COMPLETED verify task; this
    # helper is what redirects the DETERMINISTIC FAILED shapes out of the
    # crash-retry-then-merge-UNVERIFIED lane.
    artifact = {
        "detected": False,
        "passed": False,
        "verificationUnverified": True,
        "output": "no structured failure report",
    }
    assert ci_resolution.verdict_from_failed_verify_artifact(artifact) is artifact
    outcome, _ = ci_resolution.classify_merge_test_verdict(
        ci_resolution.verdict_from_failed_verify_artifact(artifact)
    )
    assert outcome == "fail"


def test_failed_verify_artifact_forces_verdict_on_clear_probe_failure():
    artifact = {
        "detected": False,
        "passed": False,
        "setupFailed": True,
        "probe": {"ran": True, "passed": False, "could_not_run": False},
    }
    assert ci_resolution.verdict_from_failed_verify_artifact(artifact) is artifact


def test_failed_verify_artifact_keeps_plain_setup_failure_on_crash_path():
    # A plain setup failure (no clear probe break, no unverified marker) is
    # transient infra noise: it must KEEP the crash-retry behavior, so no verdict
    # is forced.
    assert (
        ci_resolution.verdict_from_failed_verify_artifact(
            {
                "detected": False,
                "passed": False,
                "setupFailed": True,
                "probe": {"ran": False, "could_not_run": True},
            }
        )
        is None
    )
    assert ci_resolution.verdict_from_failed_verify_artifact(None) is None
    assert ci_resolution.verdict_from_failed_verify_artifact("not a dict") is None


# ──────────────────────────────────────────────────────────────────────────────
# classify_bypass_merge_ci — the review-BYPASS merge gate (PRs #17933/#17936/#17942)
# ──────────────────────────────────────────────────────────────────────────────


async def _classify(mocker, check_runs, *, prs=None):
    mocker.patch(
        "app.github.get_check_runs_cached", new=AsyncMock(return_value=check_runs)
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=prs or []),
    )
    return await ci_resolution.classify_bypass_merge_ci(
        77, "head-sha", token="t", owner="o", repo="r"
    )


@pytest.mark.asyncio
async def test_bypass_gate_blocks_completed_leaf_failure(mocker):
    """A present, completed, genuinely-failing LEAF check blocks the merge."""
    verdict, detail = await _classify(
        mocker,
        {
            "status": "completed",
            "conclusion": "failure",
            "details": [
                {
                    "name": "Derived artifacts",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
        },
    )
    assert verdict == ci_resolution.BYPASS_CI_BLOCKED_RED
    assert detail["state"] == "red"


@pytest.mark.asyncio
async def test_bypass_gate_does_not_inherit_the_aggregate_launder_discount(mocker):
    """A leaf red is NOT discounted just because matrix aggregates are present.

    ``github_checks._details_have_genuine_failure`` treats a check ``X`` as an
    aggregate parent when another check is named ``X (``... and discounts the
    parent's failure — the PR #16885 fix for a ``CI / Backend`` failure that
    merely laundered a cancelled shard. That discount must NOT extend to a leaf
    like ``Derived artifacts``, which has no ``Derived artifacts (`` children.

    Here a laundered aggregate (``CI / Backend`` failing over green shards) and
    a genuine leaf red co-exist. The gate must still block.
    """
    verdict, _ = await _classify(
        mocker,
        {
            "status": "completed",
            "conclusion": "failure",
            "details": [
                {"name": "CI / Backend", "status": "completed", "conclusion": "failure"},
                {
                    "name": "CI / Backend (shard 1)",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "Derived artifacts",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        },
    )
    assert verdict == ci_resolution.BYPASS_CI_BLOCKED_RED


@pytest.mark.asyncio
async def test_bypass_gate_proceeds_on_definitive_no_checks(mocker):
    """Zero check-runs keeps the pre-gate behaviour: merge proceeds.

    Deliberately NOT gated on ``proceed_on_no_checks`` — that returns False for
    a default-configured project on a non-staging branch, which would strand
    every zero-check PR (conflicted, path-filtered, or opened during an Actions
    outage) until the CI backstop FAILs it and cancels its children.
    """
    verdict, _ = await _classify(
        mocker,
        {"status": "no_checks", "conclusion": None, "details": []},
    )
    assert verdict == ci_resolution.BYPASS_CI_PROCEED
    # Pin the rationale: the strict policy helper would have refused here.
    assert ci_resolution.proceed_on_no_checks(_proj(), "automain") is False


@pytest.mark.asyncio
async def test_bypass_gate_waits_on_started_but_incomplete_check(mocker):
    """A check that has STARTED is not a check that has PASSED (#17933)."""
    verdict, detail = await _classify(
        mocker,
        {
            "status": "in_progress",
            "conclusion": None,
            "details": [
                {
                    "name": "Derived artifacts",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        },
    )
    assert verdict == ci_resolution.BYPASS_CI_WAIT_PENDING
    assert detail["state"] == "pending"


@pytest.mark.asyncio
async def test_bypass_gate_proceeds_when_check_lookup_errors(mocker):
    """An API error reports ``pending`` with EMPTY details — that is not a
    present check, so the merge proceeds rather than stalling the pipeline."""
    verdict, _ = await _classify(
        mocker,
        {
            "status": "pending",
            "conclusion": None,
            "details": [],
            "_from_api_error": True,
        },
    )
    assert verdict == ci_resolution.BYPASS_CI_PROCEED


@pytest.mark.asyncio
async def test_bypass_gate_proceeds_on_green(mocker):
    verdict, _ = await _classify(
        mocker,
        {
            "status": "completed",
            "conclusion": "success",
            "details": [
                {
                    "name": "Derived artifacts",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        },
    )
    assert verdict == ci_resolution.BYPASS_CI_PROCEED


@pytest.mark.asyncio
async def test_bypass_gate_fails_open_when_resolution_raises(mocker):
    """A bug in resolution can only ever restore the old behaviour, never wedge."""
    mocker.patch(
        "app.ci_resolution.resolve_pr_ci_status",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )
    verdict, _ = await ci_resolution.classify_bypass_merge_ci(
        77, "head-sha", token="t", owner="o", repo="r"
    )
    assert verdict == ci_resolution.BYPASS_CI_PROCEED
