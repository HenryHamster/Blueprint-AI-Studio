"""Tests for regression_layer_classifier.classify_layers()."""

from app.regression_layer_classifier import (
    ALL_LAYERS,
    LAYER_AGENT_VERB,
    LAYER_AGENT_VM,
    LAYER_BASELINE_GATE,
    LAYER_CI_WORKFLOW,
    LAYER_REVIEWER_PASS,
    build_layer_meta_block,
    classify_layers,
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
