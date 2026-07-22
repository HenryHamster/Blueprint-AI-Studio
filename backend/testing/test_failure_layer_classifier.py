"""Tests for failure_layer_classifier.classify_layers() / parse_viewport_regression_class().

Repointed from the deleted ``test_regression_layer_classifier.py`` after the
pipeline-regression apparatus was stripped: the pure classification helpers
(``classify_layers``, ``parse_viewport_regression_class``, ``ALL_LAYERS`` and
the ``LAYER_*`` constants) survive in the slim ``app.failure_layer_classifier``
module, which ``sr_blind_spots`` still consumes. The dispatcher-only helpers
(build_layer_meta_block, aggregate_targeted_layers, compute_layer_defense_counts,
compute_untargeted_layers, compute_recovery_layer_mismatch,
is_recurring_cross_layer_class) were removed with the apparatus, so their tests
were dropped — the surviving logic below is tested unchanged.
"""

from pathlib import Path

import pytest

from app.failure_layer_classifier import (
    ALL_LAYERS,
    LAYER_AGENT_VERB,
    LAYER_AGENT_VM,
    LAYER_BASELINE_GATE,
    LAYER_CI_WORKFLOW,
    LAYER_REVIEWER_PASS,
    LAYER_VIEWPORT,
    classify_layers,
    parse_viewport_regression_class,
)


# ---------------------------------------------------------------------------
# Single-layer positive cases
# ---------------------------------------------------------------------------


class TestAgentVM:
    def test_shared_library_error(self):
        msg = "error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file"
        result = classify_layers("PROVISION_FAILURE", msg)
        assert LAYER_AGENT_VM in result

    def test_playwright_system_deps(self):
        msg = "Playwright requires system dependencies that are not installed on this agent"
        result = classify_layers("BLOCKED_PRECONDITION", msg)
        assert LAYER_AGENT_VM in result

    def test_node_not_found(self):
        msg = "node: command not found"
        result = classify_layers("PROVISION_FAILURE", msg)
        assert LAYER_AGENT_VM in result

    def test_packer_build_fail(self):
        msg = "packer build failed on ami creation step"
        result = classify_layers("PROVISION_FAILURE", msg)
        assert LAYER_AGENT_VM in result

    def test_stage_provision(self):
        result = classify_layers("PROVISION_FAILURE", "", stage="provision")
        assert LAYER_AGENT_VM in result

    def test_apt_get_install(self):
        msg = "apt-get install libgbm1 failed with exit code 100"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_dash_tool_not_found(self):
        msg = "/bin/sh: 1: pytest: not found"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_docker_daemon_unavailable(self):
        msg = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_oom_exit_137(self):
        msg = "Command failed with exit code 137"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_oom_signal_killed(self):
        msg = "go test ./...\nsignal: killed"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_no_space_left_on_device(self):
        msg = "write error: No space left on device"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_disk_quota_exceeded(self):
        msg = "Error: EDQUOT: disk quota exceeded, write"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_filesystem_is_full(self):
        msg = "Filesystem is full, cannot write to /tmp"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_enospc_error_code(self):
        msg = "ENOSPC: no space left on device, write"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_permission_denied(self):
        msg = "Error: EACCES: permission denied, open /tmp/test.log"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_eacces_error_code(self):
        msg = "EACCES: permission denied when reading file"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_read_only_file_system(self):
        msg = "Error: EROFS: read-only file system, write"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result

    def test_read_only_file_system_phrase(self):
        msg = "mount: read-only file system"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VM in result


class TestCIWorkflow:
    def test_no_jobs_ran(self):
        msg = "no jobs ran for this push — path filter did not match any changed files"
        result = classify_layers("BLOCKED_PRECONDITION", msg)
        assert LAYER_CI_WORKFLOW in result

    def test_workflow_not_triggered(self):
        msg = "workflow was not triggered because the branch is excluded"
        result = classify_layers("BLOCKED_PRECONDITION", msg)
        assert LAYER_CI_WORKFLOW in result

    def test_path_filter_miss(self):
        msg = "path filter miss: backend/ not listed in on.push.paths"
        result = classify_layers("BLOCKED_PRECONDITION", msg)
        assert LAYER_CI_WORKFLOW in result

    def test_github_workflows_reference(self):
        msg = "edit .github/workflows/ci.yml to add the missing trigger"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_CI_WORKFLOW in result

    def test_stage_ci(self):
        result = classify_layers("EXTERNAL_API", "timeout", stage="ci_trigger")
        assert LAYER_CI_WORKFLOW in result


class TestAgentVerb:
    def test_scope_too_broad(self):
        msg = "scope too broad: agent modified files outside declared related_files"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VERB in result

    def test_outside_declared_scope(self):
        msg = "handler invoked outside its declared scope — admin.py not in file budget"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VERB in result

    def test_missing_check_coverage(self):
        msg = "missing ./check coverage for new handler path"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_AGENT_VERB in result

    def test_stage_handler(self):
        result = classify_layers("CODE_ERROR", "", stage="handler_dispatch")
        assert LAYER_AGENT_VERB in result


class TestBaselineGate:
    def test_eslint_errors_unchanged_files(self):
        msg = "eslint found 12 errors in unchanged files — pre-existing baseline violation"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result

    def test_ruff_error_code(self):
        msg = "ruff E501 line too long in src/foo.py"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result

    def test_module_size_limit(self):
        msg = "module_size_limits.json: backend/app/main.py exceeds 3500 line cap"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result

    def test_preexisting_lint_error(self):
        msg = "pre-existing lint errors found in admin.py — not caused by this PR"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result

    def test_stage_lint(self):
        result = classify_layers("CODE_ERROR", "", stage="lint")
        assert LAYER_BASELINE_GATE in result

    def test_stage_ruff(self):
        result = classify_layers("CODE_ERROR", "", stage="ruff_check")
        assert LAYER_BASELINE_GATE in result

    # Viewport inputs that fall through to BASELINE_GATE because they don't
    # match any VIEWPORT layer pattern (no expect*At, no .viewport.test.tsx,
    # no "viewport regression"/375px) — so the precedence guard in
    # failure_layer_classifier.classify_layers does NOT skip BASELINE_GATE here.
    # Inputs that DO match VIEWPORT patterns are covered in TestViewportLayer
    # with assertions that BASELINE_GATE is correctly suppressed.
    def test_viewport_coverage_drop(self):
        msg = "viewport coverage drop: 3 components now unguarded after PR merge"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result

    def test_check_viewport_sh_reference(self):
        msg = "check-viewport.sh exited 1 — viewport gate failed"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result


class TestReviewerPass:
    def test_reviewer_disagreement(self):
        msg = "fresh reviewer disagreed with self-review verdict on correctness"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_REVIEWER_PASS in result

    def test_reviewer_pipeline_regression(self):
        msg = "review pipeline regression detected: reviewer flags increased by 40%"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_REVIEWER_PASS in result

    def test_reviewer_recovery(self):
        msg = "reviewer recovery after incomplete SR fired for this task"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_REVIEWER_PASS in result

    def test_stage_review(self):
        result = classify_layers("CODE_ERROR", "", stage="review_pipeline")
        assert LAYER_REVIEWER_PASS in result


# ---------------------------------------------------------------------------
# VIEWPORT layer
# ---------------------------------------------------------------------------


class TestViewportLayer:
    def test_failure_class_viewport_regression(self):
        msg = "expectNoOverflowAt(375) failed: MessagesPanel overflows viewport"
        result = classify_layers("VIEWPORT_REGRESSION", msg)
        assert LAYER_VIEWPORT in result

    def test_failure_class_viewport_coverage_drop(self):
        """VIEWPORT_COVERAGE_DROP is a viewport-layer failure class and must also
        tag LAYER_VIEWPORT via _VIEWPORT_FAILURE_CLASSES — consistent with its
        sibling VIEWPORT_REGRESSION. Uses a message with NO viewport text pattern
        and a neutral stage so the failure-class membership set is what does the
        routing (the constant's canonical value is lowercase
        "viewport_coverage_drop"; classify_layers upper-cases failure_class).
        """
        result = classify_layers(
            "viewport_coverage_drop", "merged PR removed 3 assertions"
        )
        assert LAYER_VIEWPORT in result

    def test_stage_viewport_routes_to_viewport(self):
        result = classify_layers("CODE_ERROR", "", stage="viewport")
        assert LAYER_VIEWPORT in result

    def test_viewport_test_file_message(self):
        msg = "MessagesPanel.viewport.test.tsx: overflow detected at 375px"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_VIEWPORT in result

    def test_expect_no_overflow_message(self):
        msg = "expectNoOverflowAt assertion failed in KanbanBoard"
        result = classify_layers("VIEWPORT_REGRESSION", msg)
        assert LAYER_VIEWPORT in result

    def test_expect_min_touch_target_message(self):
        msg = "expectMinTouchTargetAt(375): button is only 32px tall"
        result = classify_layers("VIEWPORT_REGRESSION", msg)
        assert LAYER_VIEWPORT in result

    def test_375px_keyword(self):
        msg = "component overflows at 375px mobile width"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_VIEWPORT in result

    def test_viewport_regression_not_in_baseline_gate(self):
        # The key regression guard: viewport inputs must never also tag BASELINE_GATE.
        msg = "expectNoOverflowAt(375) failed"
        result = classify_layers("VIEWPORT_REGRESSION", msg, stage="viewport")
        assert LAYER_VIEWPORT in result
        assert LAYER_BASELINE_GATE not in result

    def test_stage_viewport_not_in_baseline_gate(self):
        result = classify_layers("CODE_ERROR", "", stage="viewport")
        assert LAYER_BASELINE_GATE not in result

    def test_lint_still_goes_to_baseline_gate(self):
        # Verify BASELINE_GATE behavior for lint inputs is unchanged.
        msg = "eslint found errors in unchanged files — pre-existing baseline violation"
        result = classify_layers("CODE_ERROR", msg, stage="lint")
        assert LAYER_BASELINE_GATE in result
        assert LAYER_VIEWPORT not in result

    def test_ruff_still_goes_to_baseline_gate(self):
        msg = "ruff E501 line too long in unchanged file"
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result
        assert LAYER_VIEWPORT not in result


# ---------------------------------------------------------------------------
# Empty-list for unclassifiable input
# ---------------------------------------------------------------------------


class TestUnclassifiable:
    def test_empty_strings(self):
        result = classify_layers("CODE_ERROR", "")
        assert result == []

    def test_generic_timeout(self):
        result = classify_layers("AGENT_TIMEOUT", "task timed out after 30 minutes")
        assert result == []

    def test_rate_limit(self):
        result = classify_layers("RATE_LIMIT", "rate limit exceeded for anthropic API")
        assert result == []

    def test_unknown_failure_class(self):
        result = classify_layers("UNKNOWN_CLASS", "some generic error message here")
        assert result == []


# ---------------------------------------------------------------------------
# Multi-layer / compound inputs
# ---------------------------------------------------------------------------


class TestMultiLayer:
    def test_packer_missing_dep_implies_both_vm_and_ci(self):
        # A packer dependency missing may also imply CI didn't rebuild the image
        msg = (
            "packer build failed: cannot open shared object file libxcb.so.1 — "
            "no jobs ran for the image rebuild workflow"
        )
        result = classify_layers("PROVISION_FAILURE", msg)
        assert LAYER_AGENT_VM in result
        assert LAYER_CI_WORKFLOW in result

    def test_lint_gate_with_baseline_and_reviewer(self):
        msg = (
            "ruff E501 errors in unchanged files triggered baseline_violation; "
            "fresh reviewer disagreed on whether to fix pre-existing errors"
        )
        result = classify_layers("CODE_ERROR", msg)
        assert LAYER_BASELINE_GATE in result
        assert LAYER_REVIEWER_PASS in result

    def test_all_results_are_valid_layer_tags(self):
        msg = (
            "error while loading shared libraries: libatk-1.0.so.0; "
            "no jobs ran in CI; scope too broad; ruff E302 in unchanged files; "
            "fresh reviewer disagreed"
        )
        result = classify_layers("PROVISION_FAILURE", msg)
        for tag in result:
            assert tag in ALL_LAYERS

    def test_no_duplicates_returned(self):
        msg = (
            "error while loading shared libraries: libatk-1.0.so.0 "
            "packer build failed on provision step"
        )
        result = classify_layers("PROVISION_FAILURE", msg, stage="provision")
        assert len(result) == len(set(result)), (
            "classify_layers returned duplicate tags"
        )


# ---------------------------------------------------------------------------
# Return-type invariants
# ---------------------------------------------------------------------------


class TestReturnType:
    def test_returns_list(self):
        result = classify_layers("CODE_ERROR", "something")
        assert isinstance(result, list)

    def test_all_tags_are_strings(self):
        msg = "error while loading shared libraries: libfoo.so.1"
        result = classify_layers("PROVISION_FAILURE", msg)
        assert all(isinstance(t, str) for t in result)

    def test_stage_none_is_fine(self):
        result = classify_layers("CODE_ERROR", "some message", stage=None)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# parse_viewport_regression_class
# ---------------------------------------------------------------------------


class TestParseViewportRegressionClass:
    def test_new_format_text_truncation(self):
        result = parse_viewport_regression_class(
            "viewport_regression:TaskCard:text_truncation@375"
        )
        assert result == {
            "component": "TaskCard",
            "assertion_type": "text_truncation",
            "width": "375",
        }

    def test_new_format_touch_target(self):
        result = parse_viewport_regression_class(
            "viewport_regression:KanbanBoard:touch_target@375"
        )
        assert result == {
            "component": "KanbanBoard",
            "assertion_type": "touch_target",
            "width": "375",
        }

    def test_new_format_overflow(self):
        result = parse_viewport_regression_class(
            "viewport_regression:DashboardHome:overflow@375"
        )
        assert result == {
            "component": "DashboardHome",
            "assertion_type": "overflow",
            "width": "375",
        }

    def test_legacy_format_no_assertion_type(self):
        result = parse_viewport_regression_class(
            "viewport_regression:MessagesPanel.tsx@375"
        )
        assert result == {
            "component": "MessagesPanel.tsx",
            "assertion_type": None,
            "width": "375",
        }

    def test_unknown_assertion_type_preserved(self):
        result = parse_viewport_regression_class(
            "viewport_regression:TaskCard:unknown@375"
        )
        assert result == {
            "component": "TaskCard",
            "assertion_type": "unknown",
            "width": "375",
        }

    def test_non_viewport_class_returns_all_none(self):
        result = parse_viewport_regression_class("fix_loop_divergence:some-fingerprint")
        assert result == {"component": None, "assertion_type": None, "width": None}

    def test_empty_string_returns_all_none(self):
        result = parse_viewport_regression_class("")
        assert result == {"component": None, "assertion_type": None, "width": None}

    def test_missing_at_sign_returns_all_none(self):
        result = parse_viewport_regression_class("viewport_regression:TaskCard")
        assert result == {"component": None, "assertion_type": None, "width": None}


# ---------------------------------------------------------------------------
# Contract / drift guard: viewport helper names must match their source of truth
# ---------------------------------------------------------------------------

def _resolve_viewport_ts() -> Path:
    candidates = (
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "test" / "viewport.ts",
        Path("/repo/admin-ui/src/test/viewport.ts"),
        Path("/admin-ui/src/test/viewport.ts"),
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


_VIEWPORT_TS = _resolve_viewport_ts()


class TestViewportClassifierSourceContract:
    """Drift guard: viewport helper names that _VIEWPORT_PATTERNS and
    _BASELINE_GATE_PATTERNS in failure_layer_classifier.py depend on
    must still be defined in admin-ui/src/test/viewport.ts.

    If a helper is renamed in TypeScript, this test fails at change-time
    with a message telling the developer to update the matching side.
    """

    @staticmethod
    def _src() -> str:
        if not _VIEWPORT_TS.exists():
            pytest.skip(
                f"{_VIEWPORT_TS} not available in this environment — the "
                "backend test image ships no admin-ui/ (build context is "
                "./backend) and CI mounts none. This viewport drift guard runs "
                "wherever the full repo tree is checked out (local dev, agent "
                "VM workspace)."
            )
        return _VIEWPORT_TS.read_text(encoding="utf-8").lower()

    def test_expect_no_overflow_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS regex 'expectNoOverflowAt' must exist in viewport.ts.

        Also matched (case-insensitively) by the _BASELINE_GATE_PATTERNS combined
        regex expect(?:NoOverflow|MinTouchTarget)At.  Renaming the helper silently
        stops viewport failures from routing to LAYER_VIEWPORT.
        """
        src = self._src()
        assert "expectnooverflowat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectNoOverflowAt'; "
            "update the matching pattern in _VIEWPORT_PATTERNS (and _BASELINE_GATE_PATTERNS) "
            "in failure_layer_classifier.py, or restore the function name in viewport.ts."
        )

    def test_expect_min_touch_target_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS regex 'expectMinTouchTargetAt' must exist in viewport.ts.

        Also matched by the _BASELINE_GATE_PATTERNS combined regex.
        """
        src = self._src()
        assert "expectmintouchtargetat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectMinTouchTargetAt'; "
            "update the matching pattern in _VIEWPORT_PATTERNS (and _BASELINE_GATE_PATTERNS) "
            "in failure_layer_classifier.py, or restore the function name in viewport.ts."
        )

    def test_expect_no_text_truncation_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS regex 'expectNoTextTruncationAt' must exist in viewport.ts."""
        src = self._src()
        assert "expectnotexttruncationat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectNoTextTruncationAt'; "
            "update the matching pattern in _VIEWPORT_PATTERNS in "
            "failure_layer_classifier.py, or restore the function name in viewport.ts."
        )
