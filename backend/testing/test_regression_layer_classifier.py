"""Tests for regression_layer_classifier.classify_layers()."""

from pathlib import Path

import pytest

from app.regression_layer_classifier import (
    ALL_LAYERS,
    LAYER_AGENT_VERB,
    LAYER_AGENT_VM,
    LAYER_BASELINE_GATE,
    LAYER_CI_WORKFLOW,
    LAYER_REVIEWER_PASS,
    LAYER_VIEWPORT,
    aggregate_targeted_layers,
    build_layer_meta_block,
    classify_layers,
    compute_layer_defense_counts,
    compute_recovery_layer_mismatch,
    compute_untargeted_layers,
    is_recurring_cross_layer_class,
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
    # no "viewport regression"/375px) — so the precedence guard at
    # regression_layer_classifier.py:288 does NOT skip BASELINE_GATE here.
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
# build_layer_meta_block
# ---------------------------------------------------------------------------


class TestBuildLayerMetaBlock:
    def test_all_layers_appear(self):
        block = build_layer_meta_block()
        for layer in ALL_LAYERS:
            assert layer in block

    def test_all_untargeted_when_none_supplied(self):
        block = build_layer_meta_block()
        assert block.count("UNTARGETED") == len(ALL_LAYERS)

    def test_targeted_marker_for_supplied_layer(self):
        block = build_layer_meta_block(targeted_layers=frozenset([LAYER_AGENT_VM]))
        assert "AGENT_VM [targeted]" in block

    def test_untargeted_marker_for_remaining_layers(self):
        block = build_layer_meta_block(targeted_layers=frozenset([LAYER_CI_WORKFLOW]))
        assert block.count("UNTARGETED") == len(ALL_LAYERS) - 1

    def test_returns_string(self):
        assert isinstance(build_layer_meta_block(), str)

    def test_descriptions_included(self):
        block = build_layer_meta_block()
        assert ".github/workflows" in block

    def test_at_least_two_layer_names_in_meta_investigation_context(self):
        block = build_layer_meta_block(
            targeted_layers=frozenset([LAYER_CI_WORKFLOW, LAYER_AGENT_VM])
        )
        layer_mentions = [layer for layer in ALL_LAYERS if layer in block]
        assert len(layer_mentions) >= 2


# ---------------------------------------------------------------------------
# compute_untargeted_layers
# ---------------------------------------------------------------------------


class TestComputeUntargetedLayers:
    def test_empty_fix_log_all_untargeted(self):
        result = compute_untargeted_layers([])
        assert set(result) == ALL_LAYERS

    def test_no_layers_targeted_field_all_untargeted(self):
        fix_log = [
            {
                "fingerprint": "cls-A",
                "fix_pr_number": 1,
                "merged_at": "2026-01-01T00:00:00Z",
            }
        ]
        result = compute_untargeted_layers(fix_log)
        assert set(result) == ALL_LAYERS

    def test_single_layer_targeted_leaves_rest_untargeted(self):
        fix_log = [{"layers_targeted": [LAYER_AGENT_VM]}]
        result = compute_untargeted_layers(fix_log)
        assert LAYER_AGENT_VM not in result
        assert len(result) == len(ALL_LAYERS) - 1

    def test_all_layers_targeted_returns_empty(self):
        fix_log = [{"layers_targeted": list(ALL_LAYERS)}]
        result = compute_untargeted_layers(fix_log)
        assert result == []

    def test_layers_targeted_across_multiple_entries(self):
        fix_log = [
            {"layers_targeted": [LAYER_CI_WORKFLOW]},
            {"layers_targeted": [LAYER_AGENT_VM, LAYER_BASELINE_GATE]},
        ]
        result = compute_untargeted_layers(fix_log)
        assert LAYER_CI_WORKFLOW not in result
        assert LAYER_AGENT_VM not in result
        assert LAYER_BASELINE_GATE not in result
        assert LAYER_AGENT_VERB in result
        assert LAYER_REVIEWER_PASS in result

    def test_unrecognized_layer_name_ignored(self):
        fix_log = [{"layers_targeted": ["UNKNOWN_LAYER", LAYER_CI_WORKFLOW]}]
        result = compute_untargeted_layers(fix_log)
        assert LAYER_CI_WORKFLOW not in result
        assert "UNKNOWN_LAYER" not in result
        assert set(result) | {LAYER_CI_WORKFLOW} == ALL_LAYERS

    def test_empty_layers_targeted_list_treated_as_unclassified(self):
        fix_log = [{"layers_targeted": []}]
        result = compute_untargeted_layers(fix_log)
        assert set(result) == ALL_LAYERS

    def test_none_layers_targeted_treated_as_unclassified(self):
        fix_log = [{"layers_targeted": None}]
        result = compute_untargeted_layers(fix_log)
        assert set(result) == ALL_LAYERS

    def test_returns_sorted_list(self):
        result = compute_untargeted_layers([])
        assert result == sorted(result)

    def test_result_subset_of_all_layers(self):
        fix_log = [{"layers_targeted": [LAYER_AGENT_VM]}]
        result = compute_untargeted_layers(fix_log)
        assert all(layer in ALL_LAYERS for layer in result)


# ---------------------------------------------------------------------------
# aggregate_targeted_layers
# ---------------------------------------------------------------------------


class TestAggregateTargetedLayers:
    def test_empty_entries_returns_empty_set(self):
        assert aggregate_targeted_layers([]) == set()

    def test_none_entries_returns_empty_set(self):
        assert aggregate_targeted_layers(None) == set()  # type: ignore[arg-type]

    def test_single_entry_single_layer(self):
        result = aggregate_targeted_layers([{"layers_targeted": [LAYER_AGENT_VM]}])
        assert result == {LAYER_AGENT_VM}

    def test_multi_entry_multi_layer_union(self):
        entries = [
            {"layers_targeted": [LAYER_CI_WORKFLOW]},
            {"layers_targeted": [LAYER_AGENT_VM, LAYER_BASELINE_GATE]},
        ]
        result = aggregate_targeted_layers(entries)
        assert result == {LAYER_CI_WORKFLOW, LAYER_AGENT_VM, LAYER_BASELINE_GATE}

    def test_unknown_layers_silently_dropped(self):
        entries = [{"layers_targeted": ["UNKNOWN_LAYER", LAYER_REVIEWER_PASS]}]
        result = aggregate_targeted_layers(entries)
        assert result == {LAYER_REVIEWER_PASS}
        assert "UNKNOWN_LAYER" not in result

    def test_entries_without_layers_targeted_skipped(self):
        entries = [{"fingerprint": "x"}, {"layers_targeted": [LAYER_AGENT_VERB]}]
        result = aggregate_targeted_layers(entries)
        assert result == {LAYER_AGENT_VERB}

    def test_non_dict_entries_skipped(self):
        entries = ["not-a-dict", {"layers_targeted": [LAYER_CI_WORKFLOW]}]
        result = aggregate_targeted_layers(entries)  # type: ignore[arg-type]
        assert result == {LAYER_CI_WORKFLOW}

    def test_result_is_subset_of_all_layers(self):
        entries = [
            {"layers_targeted": [LAYER_CI_WORKFLOW, LAYER_AGENT_VM]},
        ]
        result = aggregate_targeted_layers(entries)
        assert result <= ALL_LAYERS


# ---------------------------------------------------------------------------
# is_recurring_cross_layer_class
# ---------------------------------------------------------------------------


class TestIsRecurringCrossLayerClass:
    def _make_entry(self, layers: list[str]) -> dict:
        return {"layers_targeted": layers, "merged_at": "2026-01-01T00:00:00Z"}

    def test_trips_when_class_spans_2_layers_across_3_cycles(self):
        """3 fix cycles across CI_WORKFLOW and AGENT_VM → True."""
        entries = [
            self._make_entry([LAYER_CI_WORKFLOW]),
            self._make_entry([LAYER_AGENT_VM]),
            self._make_entry([LAYER_CI_WORKFLOW]),
        ]
        assert is_recurring_cross_layer_class(entries) is True

    def test_does_not_trip_when_single_layer(self):
        """3+ cycles but only one distinct canonical layer → False."""
        entries = [
            self._make_entry([LAYER_BASELINE_GATE]),
            self._make_entry([LAYER_BASELINE_GATE]),
            self._make_entry([LAYER_BASELINE_GATE]),
        ]
        assert is_recurring_cross_layer_class(entries) is False

    def test_does_not_trip_below_min_cycles(self):
        """Only 2 cycles even with 2 distinct layers → False (default min is 3)."""
        entries = [
            self._make_entry([LAYER_CI_WORKFLOW]),
            self._make_entry([LAYER_AGENT_VM]),
        ]
        assert is_recurring_cross_layer_class(entries) is False

    def test_custom_thresholds_respected(self):
        entries = [
            self._make_entry([LAYER_CI_WORKFLOW]),
            self._make_entry([LAYER_AGENT_VM]),
        ]
        assert (
            is_recurring_cross_layer_class(entries, min_cycles=2, min_layers=2) is True
        )

    def test_empty_entries_returns_false(self):
        assert is_recurring_cross_layer_class([]) is False

    def test_entries_without_layers_targeted_do_not_count_towards_layers(self):
        entries = [
            {"fingerprint": "x"},
            {"fingerprint": "y"},
            {"fingerprint": "z"},
        ]
        assert is_recurring_cross_layer_class(entries) is False

    def test_exactly_min_cycles_and_min_layers_trips(self):
        entries = [
            self._make_entry([LAYER_CI_WORKFLOW]),
            self._make_entry([LAYER_AGENT_VM]),
            self._make_entry([LAYER_REVIEWER_PASS]),
        ]
        assert (
            is_recurring_cross_layer_class(entries, min_cycles=3, min_layers=2) is True
        )


# ---------------------------------------------------------------------------
# compute_layer_defense_counts
# ---------------------------------------------------------------------------


class TestComputeLayerDefenseCounts:
    def test_empty_fix_log_all_zeros(self):
        result = compute_layer_defense_counts([])
        assert set(result.keys()) == ALL_LAYERS
        assert all(v == 0 for v in result.values())

    def test_all_canonical_layers_present(self):
        result = compute_layer_defense_counts([{"layers_targeted": [LAYER_AGENT_VM]}])
        assert set(result.keys()) == ALL_LAYERS

    def test_counts_incremented_per_layer(self):
        fix_log = [
            {"layers_targeted": [LAYER_AGENT_VM, LAYER_CI_WORKFLOW]},
            {"layers_targeted": [LAYER_AGENT_VM]},
        ]
        result = compute_layer_defense_counts(fix_log)
        assert result[LAYER_AGENT_VM] == 2
        assert result[LAYER_CI_WORKFLOW] == 1
        assert result[LAYER_BASELINE_GATE] == 0

    def test_unknown_layer_ignored(self):
        fix_log = [{"layers_targeted": ["UNKNOWN_LAYER", LAYER_REVIEWER_PASS]}]
        result = compute_layer_defense_counts(fix_log)
        assert result[LAYER_REVIEWER_PASS] == 1
        assert "UNKNOWN_LAYER" not in result

    def test_none_layers_targeted_treated_as_empty(self):
        fix_log = [{"layers_targeted": None}]
        result = compute_layer_defense_counts(fix_log)
        assert all(v == 0 for v in result.values())

    def test_entry_without_layers_targeted_skipped(self):
        fix_log = [{"fingerprint": "x"}, {"layers_targeted": [LAYER_VIEWPORT]}]
        result = compute_layer_defense_counts(fix_log)
        assert result[LAYER_VIEWPORT] == 1
        assert result[LAYER_AGENT_VM] == 0

    def test_non_dict_entry_skipped(self):
        fix_log = ["not-a-dict", {"layers_targeted": [LAYER_AGENT_VERB]}]
        result = compute_layer_defense_counts(fix_log)  # type: ignore[arg-type]
        assert result[LAYER_AGENT_VERB] == 1

    def test_none_fix_log_all_zeros(self):
        result = compute_layer_defense_counts(None)  # type: ignore[arg-type]
        assert set(result.keys()) == ALL_LAYERS
        assert all(v == 0 for v in result.values())


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
# compute_recovery_layer_mismatch
# ---------------------------------------------------------------------------

_TS = "2026-05-09T12:00:00+00:00"


class TestComputeRecoveryLayerMismatch:
    def _fix_entry(self, layers: list[str]) -> dict:
        return {"layers_targeted": layers, "merged_at": _TS}

    def _symptoms(self, *layers: str) -> dict:
        return {
            layer: [{"fingerprint": "fp-1", "observed_at": _TS}] for layer in layers
        }

    def test_mismatch_returns_record(self):
        fix_log = [self._fix_entry([LAYER_AGENT_VM])]
        symptoms = self._symptoms(LAYER_AGENT_VM, LAYER_CI_WORKFLOW)
        result = compute_recovery_layer_mismatch(fix_log, symptoms, evaluated_at=_TS)
        assert result is not None
        assert result["type"] == "recovery_layer_mismatch"
        assert result["evaluated_at"] == _TS
        assert result["unaddressed_layers"] == [LAYER_CI_WORKFLOW]
        assert LAYER_AGENT_VM in result["fix_layers"]
        assert LAYER_CI_WORKFLOW in result["symptom_layers"]

    def test_aligned_fix_returns_none(self):
        fix_log = [self._fix_entry([LAYER_AGENT_VM, LAYER_CI_WORKFLOW])]
        symptoms = self._symptoms(LAYER_AGENT_VM, LAYER_CI_WORKFLOW)
        assert (
            compute_recovery_layer_mismatch(fix_log, symptoms, evaluated_at=_TS) is None
        )

    def test_empty_fix_log_returns_none(self):
        symptoms = self._symptoms(LAYER_AGENT_VM)
        assert compute_recovery_layer_mismatch([], symptoms, evaluated_at=_TS) is None

    def test_none_fix_log_returns_none(self):
        symptoms = self._symptoms(LAYER_AGENT_VM)
        assert compute_recovery_layer_mismatch(None, symptoms, evaluated_at=_TS) is None

    def test_empty_symptoms_returns_none(self):
        fix_log = [self._fix_entry([LAYER_AGENT_VM])]
        assert compute_recovery_layer_mismatch(fix_log, {}, evaluated_at=_TS) is None

    def test_none_symptoms_returns_none(self):
        fix_log = [self._fix_entry([LAYER_AGENT_VM])]
        assert compute_recovery_layer_mismatch(fix_log, None, evaluated_at=_TS) is None

    def test_non_canonical_symptom_keys_ignored(self):
        fix_log = [self._fix_entry([LAYER_AGENT_VM])]
        symptoms = {LAYER_AGENT_VM: [], "UNKNOWN_LAYER": []}
        result = compute_recovery_layer_mismatch(fix_log, symptoms, evaluated_at=_TS)
        assert (
            result is None
        )  # UNKNOWN_LAYER not canonical — no unaddressed canonical layer

    def test_unaddressed_layers_sorted(self):
        fix_log = [self._fix_entry([LAYER_CI_WORKFLOW])]
        symptoms = self._symptoms(
            LAYER_CI_WORKFLOW, LAYER_AGENT_VM, LAYER_BASELINE_GATE
        )
        result = compute_recovery_layer_mismatch(fix_log, symptoms, evaluated_at=_TS)
        assert result is not None
        assert result["unaddressed_layers"] == sorted(result["unaddressed_layers"])

    def test_fix_layers_sorted(self):
        fix_log = [
            self._fix_entry([LAYER_BASELINE_GATE]),
            self._fix_entry([LAYER_AGENT_VM]),
        ]
        symptoms = self._symptoms(
            LAYER_AGENT_VM, LAYER_BASELINE_GATE, LAYER_CI_WORKFLOW
        )
        result = compute_recovery_layer_mismatch(fix_log, symptoms, evaluated_at=_TS)
        assert result is not None
        assert result["fix_layers"] == sorted(result["fix_layers"])


# ---------------------------------------------------------------------------
# Contract / drift guard: viewport helper names must match their source of truth
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]
_VIEWPORT_TS = _REPO_ROOT / "admin-ui" / "src" / "test" / "viewport.ts"


class TestViewportClassifierSourceContract:
    """Drift guard: viewport helper names that _VIEWPORT_PATTERNS and
    _BASELINE_GATE_PATTERNS in regression_layer_classifier.py depend on
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
            "in regression_layer_classifier.py, or restore the function name in viewport.ts."
        )

    def test_expect_min_touch_target_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS regex 'expectMinTouchTargetAt' must exist in viewport.ts.

        Also matched by the _BASELINE_GATE_PATTERNS combined regex.
        """
        src = self._src()
        assert "expectmintouchtargetat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectMinTouchTargetAt'; "
            "update the matching pattern in _VIEWPORT_PATTERNS (and _BASELINE_GATE_PATTERNS) "
            "in regression_layer_classifier.py, or restore the function name in viewport.ts."
        )

    def test_expect_no_text_truncation_at_in_viewport_ts(self):
        """_VIEWPORT_PATTERNS regex 'expectNoTextTruncationAt' must exist in viewport.ts."""
        src = self._src()
        assert "expectnotexttruncationat(" in src, (
            "admin-ui/src/test/viewport.ts no longer defines 'expectNoTextTruncationAt'; "
            "update the matching pattern in _VIEWPORT_PATTERNS in "
            "regression_layer_classifier.py, or restore the function name in viewport.ts."
        )
