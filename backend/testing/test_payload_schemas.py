"""Tests for backend/app/payload_schemas.py."""

import pytest

from app.payload_schemas import (
    ALL_TASK_TYPES,
    PayloadTooLargeError,
    TaskType,
    _MAX_PAYLOAD_BYTES,
    normalize_acceptance_evidence,
    parse_acceptance_evidence,
    read_acceptance_evidence,
    validate_internal_payload,
    validate_task_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid(raw: dict) -> dict:
    """Assert validation succeeds and return the result."""
    result = validate_task_payload(raw)
    assert isinstance(result, dict), "validate_task_payload must return a plain dict"
    return result


def _invalid(raw: dict) -> str:
    """Assert validation fails and return the error message."""
    with pytest.raises(ValueError) as exc_info:
        validate_task_payload(raw)
    return str(exc_info.value)


# ---------------------------------------------------------------------------
# parse_acceptance_evidence — the closed-vocabulary acceptance-evidence helper
# ---------------------------------------------------------------------------


def test_parse_acceptance_evidence_valid_test_kind_roundtrips():
    raw = {
        "kind": "test",
        "descriptor": "end-to-end seam test passes",
        "selector": "testing/test_foo.py::test_bar",
    }
    assert parse_acceptance_evidence(raw) == raw


def test_parse_acceptance_evidence_valid_without_selector():
    raw = {"kind": "behavior", "descriptor": "endpoint returns scoped audit"}
    assert parse_acceptance_evidence(raw) == raw


def test_parse_acceptance_evidence_strips_descriptor():
    out = parse_acceptance_evidence({"kind": "artifact", "descriptor": "  blob  "})
    assert out == {"kind": "artifact", "descriptor": "blob"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {
                "kind": "named_test",
                "test_path": "backend/testing/test_agent.py",
                "test_id": "test_agent_completes",
            },
            {
                "kind": "test",
                "descriptor": "test_agent_completes",
                "selector": "backend/testing/test_agent.py",
            },
        ),
        (
            {
                "kind": "observable_behavior",
                "description": "the endpoint returns scoped audit rows",
            },
            {
                "kind": "behavior",
                "descriptor": "the endpoint returns scoped audit rows",
            },
        ),
        (
            {"kind": "downstream_artifact", "artifact_id": "COMPUTE_RESULT:accuracy"},
            {"kind": "artifact", "descriptor": "COMPUTE_RESULT:accuracy"},
        ),
        (
            {"kind": "visual", "component": "MetricsDashboard", "viewport_width": 1280},
            {"kind": "visual", "descriptor": "MetricsDashboard", "selector": "1280"},
        ),
    ],
)
def test_parse_acceptance_evidence_accepts_payload_vocabulary(raw, expected):
    assert parse_acceptance_evidence(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected_payload", "expected_context"),
    [
        (
            {
                "kind": "behavior",
                "descriptor": "the endpoint returns scoped audit rows",
            },
            {
                "kind": "observable_behavior",
                "description": "the endpoint returns scoped audit rows",
            },
            {
                "kind": "behavior",
                "descriptor": "the endpoint returns scoped audit rows",
            },
        ),
        (
            {"kind": "artifact", "descriptor": "COMPUTE_RESULT:accuracy"},
            {"kind": "downstream_artifact", "artifact_id": "COMPUTE_RESULT:accuracy"},
            {"kind": "artifact", "descriptor": "COMPUTE_RESULT:accuracy"},
        ),
        (
            {
                "kind": "visual",
                "descriptor": "MetricsDashboard",
                "selector": "1280",
            },
            {"kind": "visual", "component": "MetricsDashboard", "viewport_width": 1280},
            {
                "kind": "visual",
                "descriptor": "MetricsDashboard",
                "selector": "1280",
            },
        ),
        (
            {
                "kind": "test",
                "descriptor": "seam test exercises new path",
                "selector": "backend/testing/test_agent.py::test_agent_completes",
            },
            {
                "kind": "named_test",
                "test_path": "backend/testing/test_agent.py",
                "test_id": "test_agent_completes",
            },
            {
                "kind": "test",
                "descriptor": "seam test exercises new path",
                "selector": "backend/testing/test_agent.py::test_agent_completes",
            },
        ),
    ],
)
def test_normalize_acceptance_evidence_bridges_context_to_payload(
    raw, expected_payload, expected_context
):
    assert normalize_acceptance_evidence(raw) == (expected_payload, expected_context)
    parsed = read_acceptance_evidence({"acceptance_evidence": raw})
    assert parsed is not None
    assert parsed.model_dump(exclude_none=True) == expected_payload

    result = validate_task_payload(
        {"type": "create", "prompt": "do the thing", "acceptance_evidence": raw}
    )
    assert result["acceptance_evidence"] == expected_payload


def test_normalize_acceptance_evidence_context_test_without_test_file_is_context_only():
    raw = {"kind": "test", "descriptor": "manual smoke exercise of the new path"}

    assert normalize_acceptance_evidence(raw) == (None, raw)
    assert parse_acceptance_evidence(raw) == raw
    assert read_acceptance_evidence({"acceptance_evidence": raw}) is None


def test_normalize_acceptance_evidence_visual_bad_width_is_context_only():
    raw = {"kind": "visual", "descriptor": "MetricsDashboard", "selector": "wide"}

    assert normalize_acceptance_evidence(raw) == (None, raw)
    assert parse_acceptance_evidence(raw) == raw
    assert read_acceptance_evidence({"acceptance_evidence": raw}) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-dict",
        {"kind": "screenshot", "descriptor": "nope"},
        {"kind": "behavior", "descriptor": "ok", "extra": "forbidden"},
        {"kind": "visual", "descriptor": "", "selector": "1280"},
    ],
)
def test_normalize_acceptance_evidence_malformed_is_fail_soft(raw):
    assert normalize_acceptance_evidence(raw) == (None, None)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-dict",
        42,
        {},
        {"descriptor": "no kind"},
        {"kind": "test"},  # missing descriptor
        {"kind": "screenshot", "descriptor": "bad kind"},
        {"kind": "test", "descriptor": ""},  # empty descriptor
        {"kind": "test", "descriptor": "   "},  # whitespace-only descriptor
        {"kind": "test", "descriptor": "ok", "extra": "forbidden"},  # extra key
    ],
)
def test_parse_acceptance_evidence_malformed_returns_none(raw):
    """Any absent/malformed value yields None and never raises."""
    assert parse_acceptance_evidence(raw) is None


# --- normalization: the payload vocab (named_test/…) is accepted too ----------
# Findings and plan subtasks declare acceptance_evidence in the payload vocab
# (the shape the merge gate consumes). Historically that shape was silently
# dropped from the context channel by parse_acceptance_evidence (the two
# vocabularies were disjoint on ``kind``). It is now normalized into the lighter
# context form so a single declaration populates BOTH channels.


def test_parse_acceptance_evidence_normalizes_named_test():
    out = parse_acceptance_evidence(
        {"kind": "named_test", "test_path": "testing/test_foo.py", "test_id": "test_bar"}
    )
    assert out == {
        "kind": "test",
        "descriptor": "test_bar",
        "selector": "testing/test_foo.py",
    }


def test_parse_acceptance_evidence_normalizes_named_test_inline_node():
    """A ``path::node`` test_path with no test_id still yields a descriptor."""
    out = parse_acceptance_evidence(
        {"kind": "named_test", "test_path": "testing/test_foo.py::test_bar"}
    )
    assert out == {
        "kind": "test",
        "descriptor": "test_bar",
        "selector": "testing/test_foo.py::test_bar",
    }


def test_parse_acceptance_evidence_normalizes_observable_behavior():
    out = parse_acceptance_evidence(
        {"kind": "observable_behavior", "description": "endpoint returns scoped audit"}
    )
    assert out == {"kind": "behavior", "descriptor": "endpoint returns scoped audit"}


def test_parse_acceptance_evidence_normalizes_downstream_artifact():
    out = parse_acceptance_evidence(
        {"kind": "downstream_artifact", "artifact_id": "design-doc-42"}
    )
    assert out == {"kind": "artifact", "descriptor": "design-doc-42"}


def test_parse_acceptance_evidence_normalizes_visual():
    out = parse_acceptance_evidence(
        {"kind": "visual", "component": "FleetRunnersPanel", "viewport_width": 375}
    )
    assert out == {
        "kind": "visual",
        "descriptor": "FleetRunnersPanel",
        "selector": "375",
    }


def test_parse_acceptance_evidence_rejects_malformed_payload_vocab():
    """A payload-vocab kind that fails its own validation still yields None."""
    # named_test whose test_path does not look like a real test file.
    assert parse_acceptance_evidence(
        {"kind": "named_test", "test_path": "app/main.py"}
    ) is None
    # observable_behavior with a trivially-short description.
    assert parse_acceptance_evidence(
        {"kind": "observable_behavior", "description": "ok"}
    ) is None
    # An unknown extra key is forbidden by the payload schema too.
    assert parse_acceptance_evidence(
        {"kind": "downstream_artifact", "artifact_id": "x", "extra": "no"}
    ) is None


def test_parse_acceptance_evidence_context_vocab_unchanged_by_normalization():
    """Regression guard: the lighter context vocab still round-trips byte-for-byte."""
    raw = {
        "kind": "test",
        "descriptor": "end-to-end seam test passes",
        "selector": "testing/test_foo.py::test_bar",
    }
    assert parse_acceptance_evidence(raw) == raw


# ---------------------------------------------------------------------------
# TaskType enum / ALL_TASK_TYPES
# ---------------------------------------------------------------------------

EXPECTED_TYPES = {
    "create",
    "review",
    "fix",
    "plan",
    "research",
    "strategic_review",
    "design_doc_update",
    "security_audit",
    "qa",
    "deps",
    "docs",
    "migrate",
    "smoketest",
    "refactor",
    "debug",
    "perf",
    "investigate",
    "self_audit",
    "release_audit",
    "release_audit_challenge",
    "release_merge_grade",
    "memo",
    "verify_tests",
    "compute_experiment",
    "web_research",
    "design",
    "repo_bootstrap",
    "salvage_bundle",
    "capability_proof_trial",
    "walkthrough_e2e",
}


def test_task_type_enum_has_all_expected_types():
    values = {t.value for t in TaskType}
    assert values == EXPECTED_TYPES


def test_all_task_types_frozenset_matches_enum():
    assert ALL_TASK_TYPES == EXPECTED_TYPES
    assert isinstance(ALL_TASK_TYPES, frozenset)


def test_model_for_type_covers_all_task_types():
    from app.payload_schemas import _MODEL_FOR_TYPE

    assert set(_MODEL_FOR_TYPE.keys()) == set(TaskType)


def test_model_for_type_count_matches_enum():
    from app.payload_schemas import _MODEL_FOR_TYPE

    assert len(_MODEL_FOR_TYPE) == len(TaskType)


# ---------------------------------------------------------------------------
# All 17 types pass with correct required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_type",
    [
        "create",
        "refactor",
        "plan",
        "migrate",
        "investigate",
    ],
)
def test_prompt_required_types_pass(task_type):
    result = _valid({"type": task_type, "prompt": "Do the thing"})
    assert result["type"] == task_type
    assert result["prompt"] == "Do the thing"


def test_review_passes():
    result = _valid({"type": "review", "pr_number": 42, "branch": "feature/my-branch"})
    assert result["pr_number"] == 42
    assert result["branch"] == "feature/my-branch"


def test_fix_passes():
    result = _valid(
        {"type": "fix", "prompt": "Fix it", "pr_number": 7, "branch": "main"}
    )
    assert result["prompt"] == "Fix it"
    assert result["pr_number"] == 7


@pytest.mark.parametrize(
    "task_type",
    [
        "debug",
        "research",
        "strategic_review",
        "design_doc_update",
        "security_audit",
        "qa",
        "deps",
        "docs",
        "smoketest",
        "perf",
        "self_audit",
        "release_audit_challenge",
        "release_merge_grade",
        "verify_tests",
        "compute_experiment",
    ],
)
def test_system_populated_types_pass_with_no_extra_fields(task_type):
    result = _valid({"type": task_type})
    assert result["type"] == task_type


def test_compute_experiment_payload_carries_launch_and_benchmark_fields():
    raw = {
        "type": "compute_experiment",
        "command": "python bench.py --suite full",
        "machine_type": "e2-standard-8",
        "spend_cap_usd": 2.5,
        "deadline_seconds": 1800,
        "experiment": "perf-sweep-v3",
        "benchmark": "throughput",
        "metric": "tokens_per_sec",
    }
    result = _valid(raw)
    assert result["type"] == "compute_experiment"
    assert result["command"] == "python bench.py --suite full"
    assert result["spend_cap_usd"] == 2.5
    assert result["deadline_seconds"] == 1800
    assert result["experiment"] == "perf-sweep-v3"


def test_compute_experiment_payload_carries_provenance_roundtrip():
    raw = {
        "type": "compute_experiment",
        "command": "python bench.py --suite full",
        "spend_cap_usd": 2.5,
        "deadline_seconds": 1800,
        "provenance": {
            "idea": "Tune throughput on the target GPU",
            "draft_notes": "Draft preferred a short benchmark and a tight deadline.",
            "drafted_from_refinement": True,
        },
    }
    result = _valid(raw)
    assert result["provenance"] == {
        "idea": "Tune throughput on the target GPU",
        "draft_notes": "Draft preferred a short benchmark and a tight deadline.",
        "drafted_from_refinement": True,
    }


def test_compute_experiment_payload_tolerates_extra_benchmark_fields():
    # Backward-compatible extra-field tolerance: a new benchmark field that the
    # schema does not declare must round-trip rather than be rejected/dropped.
    raw = {
        "type": "compute_experiment",
        "command": "run-eval",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 600,
        "dataset_shards": [1, 2, 3],
    }
    result = _valid(raw)
    assert result["dataset_shards"] == [1, 2, 3]


def test_compute_experiment_payload_accepts_success_criterion():
    # A well-formed goal-level acceptance criterion round-trips through the schema.
    raw = {
        "type": "compute_experiment",
        "command": "run-eval",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 600,
        "success_criterion": {
            "metric": "accuracy",
            "operator": ">=",
            "threshold": 0.9,
        },
    }
    result = _valid(raw)
    assert result["success_criterion"]["metric"] == "accuracy"
    assert result["success_criterion"]["operator"] == ">="
    assert result["success_criterion"]["threshold"] == 0.9


def test_compute_experiment_metric_criterion_requires_metric_operator_threshold():
    # kind defaults to "metric"; the metric/operator/threshold trio stays required
    # so a metric criterion missing any of them fails closed at validation.
    for missing in ("metric", "operator", "threshold"):
        criterion = {"metric": "accuracy", "operator": ">=", "threshold": 0.9}
        criterion.pop(missing)
        raw = {
            "type": "compute_experiment",
            "command": "run-eval",
            "success_criterion": criterion,
        }
        _invalid(raw)


def test_compute_experiment_artifact_produced_criterion_no_numeric_fields():
    # kind="artifact_produced" does NOT require metric/operator/threshold; the
    # value of the experiment is its durable output, not a scalar. min_file_count
    # defaults to 1 when unspecified.
    raw = {
        "type": "compute_experiment",
        "command": "train-model",
        "spend_cap_usd": 1.0,
        "deadline_seconds": 600,
        "success_criterion": {"kind": "artifact_produced"},
    }
    result = _valid(raw)
    sc = result["success_criterion"]
    assert sc["kind"] == "artifact_produced"
    assert sc["min_file_count"] == 1
    assert sc.get("metric") is None


def test_compute_experiment_artifact_produced_criterion_custom_min_file_count():
    raw = {
        "type": "compute_experiment",
        "command": "train-model",
        "success_criterion": {"kind": "artifact_produced", "min_file_count": 3},
    }
    result = _valid(raw)
    assert result["success_criterion"]["min_file_count"] == 3


def test_compute_experiment_payload_rejects_unknown_criterion_operator():
    # The operator is a closed Literal set — an unrecognized operator is rejected.
    raw = {
        "type": "compute_experiment",
        "command": "run-eval",
        "success_criterion": {
            "metric": "accuracy",
            "operator": "approx",
            "threshold": 0.9,
        },
    }
    _invalid(raw)


def test_compute_experiment_require_durable_artifact_accepted():
    raw = {
        "type": "compute_experiment",
        "command": "run-eval",
        "require_durable_artifact": True,
    }
    result = _valid(raw)
    assert result["require_durable_artifact"] is True


def test_compute_experiment_require_durable_artifact_absent_is_compatible():
    raw = {"type": "compute_experiment", "command": "run-eval"}
    result = _valid(raw)
    assert "require_durable_artifact" not in result


def test_verify_tests_payload_with_branch_and_pr_number():
    raw = {
        "type": "verify_tests",
        "branch": "feature/add-widget",
        "base_branch": "main",
        "pr_number": 42,
    }
    result = _valid(raw)
    assert result["type"] == "verify_tests"
    assert result["branch"] == "feature/add-widget"
    assert result["pr_number"] == 42


def test_verify_tests_payload_rejects_bad_branch():
    msg = _invalid({"type": "verify_tests", "branch": "-bad-branch"})
    assert "verify_tests" in msg


# ---------------------------------------------------------------------------
# Missing required fields raise ValueError with type name in message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_type",
    [
        "create",
        "refactor",
        "plan",
        "migrate",
        "investigate",
    ],
)
def test_prompt_required_types_fail_without_prompt(task_type):
    msg = _invalid({"type": task_type})
    assert task_type in msg


def test_review_fails_without_pr_number():
    msg = _invalid({"type": "review", "branch": "feature/x"})
    assert "review" in msg


def test_review_fails_without_branch():
    msg = _invalid({"type": "review", "pr_number": 1})
    assert "review" in msg


def test_fix_fails_without_prompt():
    msg = _invalid({"type": "fix", "pr_number": 1, "branch": "main"})
    assert "fix" in msg


def test_fix_fails_without_pr_number():
    msg = _invalid({"type": "fix", "prompt": "Fix it", "branch": "main"})
    assert "fix" in msg


def test_fix_fails_without_branch():
    msg = _invalid({"type": "fix", "prompt": "Fix it", "pr_number": 1})
    assert "fix" in msg


# ---------------------------------------------------------------------------
# Unknown / missing type raises ValueError
# ---------------------------------------------------------------------------


def test_unknown_type_raises():
    msg = _invalid({"type": "nonexistent_type", "prompt": "x"})
    assert "nonexistent_type" in msg


def test_missing_type_field_raises():
    with pytest.raises(ValueError) as exc_info:
        validate_task_payload({"prompt": "hello"})
    assert "type" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Extra fields are preserved (not stripped)
# ---------------------------------------------------------------------------


def test_extra_fields_preserved_create():
    raw = {
        "type": "create",
        "prompt": "Build something",
        "directions": ["some direction"],
        "vision": "Our grand vision",
        "custom_key": "custom_value",
    }
    result = _valid(raw)
    assert result["directions"] == ["some direction"]
    assert result["vision"] == "Our grand vision"
    assert result["custom_key"] == "custom_value"


def test_extra_fields_preserved_research():
    raw = {
        "type": "research",
        "design_doc": {"summary": "some doc text"},
        "research_context": {"key": "value"},
    }
    result = _valid(raw)
    assert result["design_doc"] == {"summary": "some doc text"}
    assert result["research_context"] == {"key": "value"}


# ---------------------------------------------------------------------------
# Branch name validation
# ---------------------------------------------------------------------------


def test_branch_with_dotdot_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": "main..evil"})
    assert "create" in msg


def test_branch_leading_dash_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": "-bad-branch"})
    assert "create" in msg


def test_branch_feature_slash_passes():
    result = _valid(
        {"type": "create", "prompt": "x", "base_branch": "feature/my-branch"}
    )
    assert result["base_branch"] == "feature/my-branch"


def test_review_branch_with_dotdot_rejected():
    msg = _invalid({"type": "review", "pr_number": 1, "branch": "main..evil"})
    assert "review" in msg


def test_review_branch_leading_dash_rejected():
    msg = _invalid({"type": "review", "pr_number": 1, "branch": "-bad"})
    assert "review" in msg


def test_branch_with_spaces_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": "main branch"})
    assert "create" in msg


def test_branch_with_unicode_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": "main/café"})
    assert "create" in msg


def test_branch_empty_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": ""})
    assert "create" in msg


def test_reserved_git_component_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "base_branch": "refs/.git/HEAD"})
    assert "create" in msg


# ---------------------------------------------------------------------------
# effort validation
# ---------------------------------------------------------------------------


def test_effort_ultra_rejected():
    msg = _invalid({"type": "create", "prompt": "x", "effort": "ultra"})
    assert "create" in msg


def test_effort_max_accepted():
    result = _valid({"type": "create", "prompt": "x", "effort": "max"})
    assert result["effort"] == "max"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_standard_effort_values_accepted(effort):
    result = _valid({"type": "create", "prompt": "x", "effort": effort})
    assert result["effort"] == effort


# ---------------------------------------------------------------------------
# Return type is plain dict, not Pydantic model
# ---------------------------------------------------------------------------


def test_returns_plain_dict():
    result = validate_task_payload({"type": "research"})
    assert type(result) is dict


# ---------------------------------------------------------------------------
# Backward-compat patterns from existing backend code
# ---------------------------------------------------------------------------


def test_research_completion_pattern():
    """Pattern used by research_completion.py: type + prompt + base_branch + optional fields."""
    raw = {
        "type": "create",
        "prompt": "Add feature X",
        "base_branch": "main",
        "model": "claude-opus-4-6",
        "effort": "high",
        "prompt_suffix": "Be concise.",
    }
    result = _valid(raw)
    assert result["prompt"] == "Add feature X"
    assert result["base_branch"] == "main"
    assert result["model"] == "claude-opus-4-6"
    assert result["effort"] == "high"
    assert result["prompt_suffix"] == "Be concise."


def test_review_pipeline_pattern():
    """Pattern used by review_pipeline.py: type=review + pr_number + branch."""
    raw = {"type": "review", "pr_number": 123, "branch": "feature/something"}
    result = _valid(raw)
    assert result["pr_number"] == 123
    assert result["branch"] == "feature/something"


def test_autonomous_research_pattern():
    """Pattern used by autonomous_research.py: type=research with no required fields."""
    raw = {"type": "research"}
    result = _valid(raw)
    assert result["type"] == "research"


def test_fix_pipeline_pattern():
    """Pattern used by review_pipeline.py for fix tasks."""
    raw = {
        "type": "fix",
        "prompt": "Address review comments",
        "pr_number": 55,
        "branch": "fix/issue-55",
    }
    result = _valid(raw)
    assert result["prompt"] == "Address review comments"
    assert result["pr_number"] == 55


# ---------------------------------------------------------------------------
# validate_internal_payload — fail-closed on schema/required-field violations
# ---------------------------------------------------------------------------


def test_internal_payload_passes_valid():
    raw = {"type": "create", "prompt": "Do it"}
    result = validate_internal_payload(raw, "test_context")
    assert result["prompt"] == "Do it"


def test_internal_payload_raises_on_missing_required_field():
    """Caller bugs (missing required fields) must surface, not get silently
    rewritten into an empty payload that crashes the agent. Regression guard
    for incident 2026-05-07 task 8c1ab616 — a fix-spawn site forgot
    ``fix_prompt``, the validator silently passed the empty ``prompt: ""``
    through, and the agent died with ``Fatal error: Fix task missing prompt``
    after burning 3 retries.
    """
    raw = {
        "type": "fix",
        "pr_number": 42,
        "branch": "blueprint/task-x",
    }  # missing prompt
    with pytest.raises(ValueError, match="prompt"):
        validate_internal_payload(raw, "my_context")


def test_internal_payload_raises_on_empty_required_string():
    """min_length=1 violations also indicate caller bugs and must raise."""
    raw = {"type": "fix", "pr_number": 42, "branch": "blueprint/task-x", "prompt": ""}
    with pytest.raises(ValueError):
        validate_internal_payload(raw, "ctx_empty_prompt")


def test_internal_payload_raises_on_unknown_type():
    raw = {"type": "totally_unknown_type"}
    with pytest.raises(ValueError, match="Unknown task type"):
        validate_internal_payload(raw, "ctx_label")


def test_internal_payload_raises_on_constraint_violation():
    """gt=0 / pattern violations also surface as caller bugs."""
    raw = {"type": "review", "pr_number": -1, "branch": "main"}
    with pytest.raises(ValueError):
        validate_internal_payload(raw, "neg_pr_number")


# ---------------------------------------------------------------------------
# New Optional typed fields — correct types accepted
# ---------------------------------------------------------------------------


def test_research_payload_optional_fields_accepted():
    raw = {
        "type": "research",
        "directions": [{"id": "d1", "title": "Improve X"}],
        "research_mode": "standard",
        "current_direction_id": "d-abc123",
        "anomaly_signals": [{"signal": "high error rate"}],
        "project_name": "MyProject",
        "design_doc": {"summary": "Current design"},
    }
    result = _valid(raw)
    assert result["directions"] == [{"id": "d1", "title": "Improve X"}]
    assert result["research_mode"] == "standard"
    assert result["current_direction_id"] == "d-abc123"
    assert result["anomaly_signals"] == [{"signal": "high error rate"}]
    assert result["project_name"] == "MyProject"
    assert result["design_doc"] == {"summary": "Current design"}


def test_research_payload_omit_optional_fields_passes():
    result = _valid({"type": "research"})
    assert result["type"] == "research"
    assert "directions" not in result
    assert "research_mode" not in result
    assert "current_direction_id" not in result


def test_research_payload_directions_wrong_type_rejected():
    msg = _invalid({"type": "research", "directions": "not-a-list"})
    assert "research" in msg


def test_research_payload_design_doc_wrong_type_rejected():
    msg = _invalid({"type": "research", "design_doc": "not-a-dict"})
    assert "research" in msg


def test_design_doc_update_optional_fields_accepted():
    raw = {
        "type": "design_doc_update",
        "design_doc": {"summary": "Current design"},
        "vision": {"text": "Our vision"},
        "open_messages": [{"id": "m1", "body": "Question?"}],
        "project_name": "MyProject",
        "proposed_changes": [{"change": "Add feature"}],
    }
    result = _valid(raw)
    assert result["design_doc"] == {"summary": "Current design"}
    assert result["vision"] == {"text": "Our vision"}
    assert result["open_messages"] == [{"id": "m1", "body": "Question?"}]
    assert result["project_name"] == "MyProject"
    assert result["proposed_changes"] == [{"change": "Add feature"}]


def test_design_doc_update_omit_optional_fields_passes():
    result = _valid({"type": "design_doc_update"})
    assert result["type"] == "design_doc_update"
    assert "design_doc" not in result
    assert "vision" not in result


def test_design_doc_update_open_messages_wrong_type_rejected():
    msg = _invalid({"type": "design_doc_update", "open_messages": "not-a-list"})
    assert "design_doc_update" in msg


def test_strategic_review_optional_fields_accepted():
    raw = {
        "type": "strategic_review",
        "design_doc": {"summary": "doc"},
        "vision": {"text": "vision"},
        "directions_summary": [{"id": "d1"}],
        "outcome_evaluation": {"score": 0.9},
        "open_messages": [{"id": "m1"}],
        "project_name": "MyProject",
        "research_mode": "strategic",
        "max_active_directions": 10,
        "capability_trial_verdicts": {"cap-a": {"verdict": "adopt"}},
    }
    result = _valid(raw)
    assert result["design_doc"] == {"summary": "doc"}
    assert result["vision"] == {"text": "vision"}
    assert result["directions_summary"] == [{"id": "d1"}]
    assert result["outcome_evaluation"] == {"score": 0.9}
    assert result["open_messages"] == [{"id": "m1"}]
    assert result["project_name"] == "MyProject"
    assert result["research_mode"] == "strategic"
    assert result["max_active_directions"] == 10
    assert result["capability_trial_verdicts"] == {"cap-a": {"verdict": "adopt"}}


def test_strategic_review_omit_optional_fields_passes():
    result = _valid({"type": "strategic_review"})
    assert result["type"] == "strategic_review"
    assert "design_doc" not in result
    assert "max_active_directions" not in result


def test_strategic_review_max_active_directions_wrong_type_rejected():
    msg = _invalid({"type": "strategic_review", "max_active_directions": "ten"})
    assert "strategic_review" in msg


def test_smoketest_health_check_urls_accepted():
    raw = {"type": "smoketest", "health_check_urls": ["https://example.com/health"]}
    result = _valid(raw)
    assert result["health_check_urls"] == ["https://example.com/health"]


def test_smoketest_omit_optional_fields_passes():
    result = _valid({"type": "smoketest"})
    assert result["type"] == "smoketest"
    assert "health_check_urls" not in result


def test_smoketest_health_check_urls_wrong_type_rejected():
    msg = _invalid({"type": "smoketest", "health_check_urls": "not-a-list"})
    assert "smoketest" in msg


def test_debug_optional_fields_accepted():
    raw = {
        "type": "debug",
        "failed_task_id": "task-abc",
        "failed_task_title": "Some failing task",
        "error_logs": "ERROR: something went wrong",
        "failed_task_prompt": "Do X",
        "failed_task_type": "create",
    }
    result = _valid(raw)
    assert result["failed_task_id"] == "task-abc"
    assert result["failed_task_title"] == "Some failing task"
    assert result["error_logs"] == "ERROR: something went wrong"
    assert result["failed_task_prompt"] == "Do X"
    assert result["failed_task_type"] == "create"


def test_debug_omit_optional_fields_passes():
    result = _valid({"type": "debug"})
    assert result["type"] == "debug"
    assert "failed_task_id" not in result


def test_debug_failed_task_id_wrong_type_rejected():
    msg = _invalid({"type": "debug", "failed_task_id": 123})
    assert "debug" in msg


def test_review_optional_task_fields_accepted():
    raw = {
        "type": "review",
        "pr_number": 42,
        "branch": "feature/x",
        "original_task_id": "task-123",
        "original_task_type": "create",
    }
    result = _valid(raw)
    assert result["original_task_id"] == "task-123"
    assert result["original_task_type"] == "create"


def test_review_omit_optional_task_fields_passes():
    result = _valid({"type": "review", "pr_number": 1, "branch": "main"})
    assert "original_task_id" not in result
    assert "original_task_type" not in result


def test_fix_optional_fields_accepted():
    raw = {
        "type": "fix",
        "prompt": "Fix it",
        "pr_number": 7,
        "branch": "main",
        "original_task_id": "task-456",
        "original_prompt": "Original request",
    }
    result = _valid(raw)
    assert result["original_task_id"] == "task-456"
    assert result["original_prompt"] == "Original request"


def test_fix_omit_optional_fields_passes():
    result = _valid(
        {"type": "fix", "prompt": "Fix it", "pr_number": 1, "branch": "main"}
    )
    assert "original_task_id" not in result
    assert "original_prompt" not in result


def test_perf_prompt_optional_accepted():
    raw = {
        "type": "perf",
        "prompt": "Profile the hot path",
        "max_severity": "medium",
    }
    result = _valid(raw)
    assert result["prompt"] == "Profile the hot path"
    assert result["max_severity"] == "medium"


def test_perf_omit_prompt_passes():
    result = _valid({"type": "perf"})
    assert result["type"] == "perf"
    assert "prompt" not in result


def test_security_audit_prompt_optional_accepted():
    raw = {"type": "security_audit", "prompt": "Audit auth endpoints"}
    result = _valid(raw)
    assert result["prompt"] == "Audit auth endpoints"


def test_security_audit_omit_prompt_passes():
    result = _valid({"type": "security_audit"})
    assert result["type"] == "security_audit"
    assert "prompt" not in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_pr_number_zero_rejected_for_review():
    msg = _invalid({"type": "review", "pr_number": 0, "branch": "main"})
    assert "review" in msg


def test_pr_number_negative_rejected_for_fix():
    msg = _invalid({"type": "fix", "prompt": "x", "pr_number": -5, "branch": "main"})
    assert "fix" in msg


def test_empty_prompt_rejected():
    msg = _invalid({"type": "create", "prompt": ""})
    assert "create" in msg


def test_type_preserved_in_output():
    """The 'type' field must always be present in the returned dict."""
    for t in ["create", "review", "research", "fix"]:
        raw: dict = {"type": t}
        if t in ("create",):
            raw["prompt"] = "x"
        if t == "review":
            raw.update({"pr_number": 1, "branch": "main"})
        if t == "fix":
            raw.update({"prompt": "x", "pr_number": 1, "branch": "main"})
        result = validate_task_payload(raw)
        assert result["type"] == t, f"'type' missing from result for {t!r}"


# ---------------------------------------------------------------------------
# Payload-size budget (prevents silent pre-claim death from oversized state)
# ---------------------------------------------------------------------------


def test_oversized_payload_raises_payload_too_large_error():
    """Payloads larger than _MAX_PAYLOAD_BYTES must fail validation hard."""
    # Build a strategic_review payload with a huge design_doc field. Using
    # strategic_review because its schema accepts dict-valued design_doc via
    # extra='allow' without triggering per-type required-field errors.
    big_blob = {"payload_archive": ["x" * 100] * 5000}  # ~500KB
    raw = {"type": "strategic_review", "design_doc": big_blob}
    with pytest.raises(PayloadTooLargeError) as exc_info:
        validate_task_payload(raw)
    err = exc_info.value
    assert err.size > _MAX_PAYLOAD_BYTES
    assert err.limit == _MAX_PAYLOAD_BYTES
    # Top-keys breakdown should identify the offending field
    top_key_names = [k for k, _ in err.top_keys]
    assert "design_doc" in top_key_names
    # Error message includes size and the largest field for log diagnostics
    msg = str(err)
    assert "exceeds limit" in msg
    assert "design_doc" in msg


def test_oversized_payload_is_value_error_subclass():
    """PayloadTooLargeError must remain a ValueError subclass so legacy
    except-ValueError sites keep working."""
    assert issubclass(PayloadTooLargeError, ValueError)


def test_normal_payload_passes_size_check():
    """A realistic strategic_review payload should be well under the budget."""
    raw = {
        "type": "strategic_review",
        "design_doc": {"summary": "ok", "systems/sys-a": {"id": "sys-a"}},
        "vision": {"focus": ["ship"]},
    }
    result = validate_task_payload(raw)
    assert result["type"] == "strategic_review"


def test_validate_internal_payload_reraises_size_errors():
    """validate_internal_payload is fail-open for schema errors but MUST
    re-raise size errors — silently queueing an oversized payload is the
    exact scenario the budget is trying to prevent."""
    big_blob = {"data": ["x" * 100] * 5000}
    raw = {"type": "strategic_review", "design_doc": big_blob}
    with pytest.raises(PayloadTooLargeError):
        validate_internal_payload(raw, context="test")


def test_validate_internal_payload_raises_on_schema_errors():
    """Schema errors must surface — fail-closed prevents silent caller bugs
    (incident 2026-05-07 task 8c1ab616: empty fix_prompt slipped past
    fail-open validation, agent crashed after 3 retries)."""
    # Missing required 'prompt' for a create task. Should raise.
    raw = {"type": "create"}
    with pytest.raises(ValueError):
        validate_internal_payload(raw, context="test")


# ---------------------------------------------------------------------------
# SelfAuditPayload
# ---------------------------------------------------------------------------


def test_self_audit_passes_with_no_fields():
    result = _valid({"type": "self_audit"})
    assert result["type"] == "self_audit"
    assert "prompt" not in result
    assert "focus_systems" not in result
    assert "max_systems" not in result


def test_self_audit_prompt_accepted():
    result = _valid({"type": "self_audit", "prompt": "Audit the auth subsystem"})
    assert result["prompt"] == "Audit the auth subsystem"


def test_self_audit_focus_systems_accepted():
    result = _valid({"type": "self_audit", "focus_systems": ["auth", "billing"]})
    assert result["focus_systems"] == ["auth", "billing"]


def test_self_audit_max_systems_accepted():
    result = _valid({"type": "self_audit", "max_systems": 5})
    assert result["max_systems"] == 5


def test_self_audit_all_optional_fields_accepted():
    raw = {
        "type": "self_audit",
        "prompt": "Full audit",
        "focus_systems": ["sys-a", "sys-b"],
        "max_systems": 3,
    }
    result = _valid(raw)
    assert result["prompt"] == "Full audit"
    assert result["focus_systems"] == ["sys-a", "sys-b"]
    assert result["max_systems"] == 3


def test_self_audit_focus_systems_wrong_type_rejected():
    msg = _invalid({"type": "self_audit", "focus_systems": "not-a-list"})
    assert "self_audit" in msg


def test_self_audit_max_systems_wrong_type_rejected():
    msg = _invalid({"type": "self_audit", "max_systems": "five"})
    assert "self_audit" in msg


def test_self_audit_oversized_payload_raises():
    big_blob = {"data": ["x" * 100] * 5000}
    raw = {"type": "self_audit", "prompt": "x", "extra_blob": big_blob}
    with pytest.raises(PayloadTooLargeError):
        validate_task_payload(raw)


# ---------------------------------------------------------------------------
# base_branch must not silently default to "main"
# ---------------------------------------------------------------------------


def test_base_branch_absent_not_defaulted_to_main():
    """Omitting base_branch must NOT silently inject 'main' into the result.

    Regression guard: BasePayload previously declared base_branch='main',
    masking spawn-site bugs on repos whose integration branch isn't main.
    """
    result = _valid({"type": "research"})
    assert "base_branch" not in result, (
        f"base_branch should be absent when not supplied, got {result.get('base_branch')!r}"
    )


def test_base_branch_none_excluded_from_dump():
    """A payload validated without base_branch must not emit base_branch=None."""
    result = _valid({"type": "create", "prompt": "Do the thing"})
    assert "base_branch" not in result


def test_base_branch_explicit_value_preserved():
    """Explicitly supplied base_branch values are still accepted and returned."""
    result = _valid({"type": "create", "prompt": "x", "base_branch": "trunk"})
    assert result["base_branch"] == "trunk"


# ---------------------------------------------------------------------------
# Registry-tolerant fallback for unregistered payload models
# ---------------------------------------------------------------------------


def test_registered_type_without_bespoke_model_uses_base_payload():
    """When a task type is registered in the enum but absent from
    _MODEL_FOR_TYPE, validation should fall back to BasePayload
    (extra='allow' + shared constraints) rather than crashing with KeyError.

    This enables new task types to be runnable by registration alone.
    """
    from app.payload_schemas import _MODEL_FOR_TYPE

    # Temporarily remove a system-populated type from the dispatch to simulate
    # a newly-registered type that hasn't been added to the dispatch yet.
    original = _MODEL_FOR_TYPE.pop(TaskType.debug, None)
    try:
        # Should validate against BasePayload, not crash with KeyError
        result = _valid({"type": "debug"})
        assert result["type"] == "debug"

        # Extra fields should be preserved (BasePayload has extra='allow')
        result = _valid({"type": "debug", "custom_field": "custom_value"})
        assert result["custom_field"] == "custom_value"

        # Base-payload constraints (e.g., effort pattern) still apply
        msg = _invalid({"type": "debug", "effort": "invalid_effort"})
        assert "debug" in msg
    finally:
        # Restore the original entry
        if original is not None:
            _MODEL_FOR_TYPE[TaskType.debug] = original


def test_registered_type_without_model_still_enforces_size_budget():
    """When a type falls back to BasePayload, the size-budget gate
    must still reject payloads that exceed _MAX_PAYLOAD_BYTES.
    """
    from app.payload_schemas import _MODEL_FOR_TYPE

    # Temporarily remove a type to force BasePayload fallback
    original = _MODEL_FOR_TYPE.pop(TaskType.qa, None)
    try:
        # Build an oversized payload
        big_blob = {"data": ["x" * 100] * 5000}
        raw = {"type": "qa", "custom_huge_field": big_blob}

        # Should fail on size, not validate silently
        with pytest.raises(PayloadTooLargeError) as exc_info:
            validate_task_payload(raw)
        err = exc_info.value
        assert err.size > _MAX_PAYLOAD_BYTES
        assert "custom_huge_field" in [k for k, _ in err.top_keys]
    finally:
        if original is not None:
            _MODEL_FOR_TYPE[TaskType.qa] = original


# ---------------------------------------------------------------------------
# SystemMaturity ORM constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_maturity_unique_constraint_project_system(db_session):
    """Two SystemMaturity rows with the same (project_id, system_id) must violate the unique constraint."""
    from sqlalchemy.exc import IntegrityError
    from app.models import SystemMaturity
    from testing.factories import make_project

    project = make_project(
        name="Maturity Constraint Test", user_id="test-user-maturity"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    system_id = "sys-unique-constraint-check"
    row1 = SystemMaturity(
        project_id=project.id, system_id=system_id, score=80, recommended_mode="ship"
    )
    db_session.add(row1)
    await db_session.commit()

    row2 = SystemMaturity(
        project_id=project.id, system_id=system_id, score=50, recommended_mode="audit"
    )
    db_session.add(row2)
    with pytest.raises(IntegrityError):
        await db_session.commit()


# ---------------------------------------------------------------------------
# AcceptanceEvidence — the general plan-time declaration contract
# ---------------------------------------------------------------------------

# The code/PR-producing task types that carry the optional acceptance_evidence
# field (including ``plan``, which forward-carries a finding's declared evidence
# through decomposition). Each entry maps the task type to the minimal extra
# fields needed to
# satisfy its own required-field schema (so we can build a complete payload).
_ACCEPTANCE_TYPES: dict[str, dict] = {
    "create": {"prompt": "do the thing"},
    "plan": {"prompt": "plan the thing"},
    "fix": {"prompt": "fix it", "pr_number": 7, "branch": "fix/x"},
    "refactor": {"prompt": "refactor it"},
    "qa": {},
    "migrate": {"prompt": "add a column"},
    "deps": {},
    "docs": {},
}

_VALID_EVIDENCE = {
    "named_test": {
        "kind": "named_test",
        "test_path": "backend/testing/test_agent.py",
        "test_id": "test_agent_completes",
    },
    "named_test_inline_node": {
        "kind": "named_test",
        "test_path": "backend/testing/test_agent.py::test_agent_completes",
    },
    "observable_behavior": {
        "kind": "observable_behavior",
        "description": "the merge gate blocks a self-skipping PR",
    },
    "downstream_artifact": {
        "kind": "downstream_artifact",
        "artifact_id": "COMPUTE_RESULT:accuracy",
    },
    "visual": {
        "kind": "visual",
        "component": "MetricsDashboard",
        "viewport_width": 1280,
    },
}


@pytest.mark.parametrize("task_type", sorted(_ACCEPTANCE_TYPES))
@pytest.mark.parametrize("evidence_key", sorted(_VALID_EVIDENCE))
def test_acceptance_evidence_valid_kinds_round_trip(task_type, evidence_key):
    """Each code/PR-producing type accepts a valid declaration of each kind and round-trips it."""
    evidence = dict(_VALID_EVIDENCE[evidence_key])
    raw = {
        "type": task_type,
        **_ACCEPTANCE_TYPES[task_type],
        "acceptance_evidence": evidence,
    }
    result = _valid(raw)
    assert result["acceptance_evidence"]["kind"] == evidence["kind"]
    # Optional sub-fields that were None are dropped by exclude_none; declared
    # ones survive verbatim.
    for k, v in evidence.items():
        assert result["acceptance_evidence"][k] == v


@pytest.mark.parametrize("task_type", sorted(_ACCEPTANCE_TYPES))
def test_acceptance_evidence_absent_is_legacy_compatible(task_type):
    """A payload with no acceptance_evidence validates and omits the field entirely (legacy)."""
    raw = {"type": task_type, **_ACCEPTANCE_TYPES[task_type]}
    result = _valid(raw)
    assert "acceptance_evidence" not in result, (
        "exclude_none must drop the unset field so legacy payloads round-trip identically"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        # named_test pointing at a path that is not a test file
        {"kind": "named_test", "test_path": "backend/app/payload_schemas.py"},
        # bare 'test' placeholder path (not a file)
        {"kind": "named_test", "test_path": "test"},
        # empty test_path
        {"kind": "named_test", "test_path": ""},
        # real test file but a placeholder/always-true test reference
        {
            "kind": "named_test",
            "test_path": "backend/testing/test_agent.py",
            "test_id": "assert True",
        },
        {"kind": "named_test", "test_path": "backend/testing/test_x.py::test_true"},
        # observable_behavior with an empty / trivial description
        {"kind": "observable_behavior", "description": ""},
        {"kind": "observable_behavior", "description": "   "},
        {"kind": "observable_behavior", "description": "works"},
        {"kind": "observable_behavior", "description": "assert True"},
        # downstream_artifact with no / trivial id
        {"kind": "downstream_artifact", "artifact_id": ""},
        {"kind": "downstream_artifact", "artifact_id": "placeholder"},
    ],
)
def test_acceptance_evidence_trivial_declarations_rejected(evidence):
    """Trivial / always-true / non-test declarations fail closed at validation time."""
    raw = {"type": "create", "prompt": "p", "acceptance_evidence": evidence}
    _invalid(raw)


def test_acceptance_evidence_unknown_kind_rejected():
    raw = {
        "type": "create",
        "prompt": "p",
        "acceptance_evidence": {"kind": "vibes", "description": "trust me"},
    }
    _invalid(raw)


def test_acceptance_evidence_viewport_width_bound_from_constants():
    """The visual viewport_width bound is sourced from the canonical
    VIEWPORT_WIDTH_MIN/MAX constants (viewport_evidence), so a declared width
    outside those bounds fails closed at validation time — keeping this
    declaration-time bound in lock-step with the upload endpoint's Field(ge,le).
    """
    from app.viewport_evidence import VIEWPORT_WIDTH_MAX, VIEWPORT_WIDTH_MIN

    def _visual(width):
        return {
            "type": "create",
            "prompt": "p",
            "acceptance_evidence": {
                "kind": "visual",
                "component": "MetricsDashboard",
                "viewport_width": width,
            },
        }

    # Boundary values inside [MIN, MAX] validate.
    _valid(_visual(VIEWPORT_WIDTH_MIN))
    _valid(_visual(VIEWPORT_WIDTH_MAX))
    # Just outside either bound fails closed — proving the bound IS the constant,
    # not a wider/absent limit.
    _invalid(_visual(VIEWPORT_WIDTH_MIN - 1))
    _invalid(_visual(VIEWPORT_WIDTH_MAX + 1))


def test_acceptance_evidence_extra_field_forbidden():
    """AcceptanceEvidence is a closed shape (extra='forbid')."""
    raw = {
        "type": "create",
        "prompt": "p",
        "acceptance_evidence": {
            "kind": "observable_behavior",
            "description": "the gate blocks a bad PR",
            "bogus": "x",
        },
    }
    _invalid(raw)


def test_read_acceptance_evidence_accessor():
    """read_acceptance_evidence parses a well-formed declaration and is fail-soft otherwise."""
    from app.payload_schemas import AcceptanceEvidence, read_acceptance_evidence

    parsed = read_acceptance_evidence(
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_agent.py",
            }
        }
    )
    assert isinstance(parsed, AcceptanceEvidence)
    assert parsed.kind == "named_test"

    # Absent / malformed / wrong-type all read as None rather than raising.
    assert read_acceptance_evidence(None) is None
    assert read_acceptance_evidence({}) is None
    assert read_acceptance_evidence({"acceptance_evidence": None}) is None
    assert read_acceptance_evidence({"acceptance_evidence": "nope"}) is None
    assert (
        read_acceptance_evidence(
            {
                "acceptance_evidence": {
                    "kind": "named_test",
                    "test_path": "backend/app/main.py",
                }
            }
        )
        is None
    )


def test_acceptance_evidence_round_trips_through_model_instance():
    """The accessor accepts an already-parsed AcceptanceEvidence instance."""
    from app.payload_schemas import AcceptanceEvidence, read_acceptance_evidence

    ev = AcceptanceEvidence(kind="downstream_artifact", artifact_id="KNOWLEDGE_DOC:42")
    assert read_acceptance_evidence({"acceptance_evidence": ev}) is ev


# ---------------------------------------------------------------------------
# visual kind — first-class viewport screenshot acceptance evidence
# ---------------------------------------------------------------------------


def test_visual_evidence_kind_validates_and_rejects_placeholder():
    """Visual acceptance_evidence validates substantive declarations and rejects
    empty/placeholder component names and missing/out-of-range widths.

    Also verifies that the component-key derivation round-trips through the
    shared viewport_object_name helper — the same function used by the agent
    upload endpoint and the admin download endpoint — so a declared
    (component, width) pair uniquely identifies a GCS object with no ambiguity
    and no risk of the upload/download key-mismatch bug from PR #9522.
    """
    from app.payload_schemas import AcceptanceEvidence
    from app.viewport_evidence import viewport_object_name

    # ── valid declaration validates and round-trips ────────────────────────
    ev = AcceptanceEvidence(
        kind="visual", component="MetricsDashboard", viewport_width=1280
    )
    assert ev.kind == "visual"
    assert ev.component == "MetricsDashboard"
    assert ev.viewport_width == 1280
    assert ev.assertion_type is None  # optional, absent by default

    # Optional assertion_type is accepted when supplied.
    ev2 = AcceptanceEvidence(
        kind="visual",
        component="ProjectDashboard",
        viewport_width=800,
        assertion_type="chart colours match design",
    )
    assert ev2.assertion_type == "chart colours match design"

    # ── shared helper produces the same key the endpoints use ─────────────
    # Upload and download endpoints were previously duplicating this logic;
    # they now both call viewport_object_name.  Anchor the derivation to verify
    # byte-for-byte consistency with what the agent would upload and what the
    # admin panel would download.
    project_id = "proj-abc123"
    assert viewport_object_name(project_id, "MetricsDashboard", 1280) == (
        f"viewport-failures/{project_id}/MetricsDashboard-1280.png"
    )
    # Characters outside [A-Za-z0-9._-] are sanitised to underscores, same as
    # the regex previously inlined in agent.py and main.py.
    assert viewport_object_name(project_id, "My Component!", 800) == (
        f"viewport-failures/{project_id}/My_Component_-800.png"
    )
    # Component truncated at 100 chars.
    long_name = "A" * 150
    obj = viewport_object_name(project_id, long_name, 1024)
    assert obj == f"viewport-failures/{project_id}/{'A' * 100}-1024.png"

    # ── placeholder / empty component rejected ────────────────────────────
    for bad_component in ("", "   ", "placeholder", "test", "todo", "none", "n/a"):
        with pytest.raises(Exception):
            AcceptanceEvidence(
                kind="visual", component=bad_component, viewport_width=1280
            )

    # ── missing viewport_width rejected ──────────────────────────────────
    with pytest.raises(Exception):
        AcceptanceEvidence(kind="visual", component="MetricsDashboard")

    # ── out-of-range viewport_width rejected (same bounds as upload endpoint)
    with pytest.raises(Exception):
        AcceptanceEvidence(
            kind="visual", component="MetricsDashboard", viewport_width=0
        )
    with pytest.raises(Exception):
        AcceptanceEvidence(
            kind="visual", component="MetricsDashboard", viewport_width=5000
        )

    # ── extra='forbid' discipline preserved ──────────────────────────────
    raw_extra = {
        "type": "create",
        "prompt": "add the component",
        "acceptance_evidence": {
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
            "unexpected_field": "x",
        },
    }
    _invalid(raw_extra)

    # ── full validate_task_payload round-trip ─────────────────────────────
    raw = {
        "type": "create",
        "prompt": "implement the component",
        "acceptance_evidence": {
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
    }
    result = _valid(raw)
    assert result["acceptance_evidence"]["kind"] == "visual"
    assert result["acceptance_evidence"]["component"] == "MetricsDashboard"
    assert result["acceptance_evidence"]["viewport_width"] == 1280


def test_visual_evidence_parse_acceptance_evidence_context_channel():
    """The lighter PlanAcceptanceEvidence context-channel accepts kind='visual'
    (descriptor = component name, selector = viewport width string)."""
    raw = {
        "kind": "visual",
        "descriptor": "MetricsDashboard",
        "selector": "1280",
    }
    result = parse_acceptance_evidence(raw)
    assert result is not None
    assert result["kind"] == "visual"
    assert result["descriptor"] == "MetricsDashboard"
    assert result["selector"] == "1280"


def test_visual_evidence_resolve_found_and_missing():
    """resolve_visual_evidence returns verified when the context URI matches and
    unverified when absent — never silently counts as verified."""
    from app.viewport_evidence import resolve_visual_evidence, viewport_object_name

    project_id = "proj-xyz"
    component = "MetricsDashboard"
    width = 1280
    expected_obj = viewport_object_name(project_id, component, width)

    # URI present and matching → verified.
    ctx_match = {"viewport_failure_screenshot_uri": f"gs://my-bucket/{expected_obj}"}
    result = resolve_visual_evidence(project_id, component, width, ctx_match)
    assert result["resolved"] is True
    assert result["status"] == "verified"
    assert result["object_name"] == expected_obj

    # URI absent → unverified.
    result_missing = resolve_visual_evidence(project_id, component, width, {})
    assert result_missing["resolved"] is False
    assert result_missing["status"] == "unverified"
    assert result_missing["uri"] is None

    # URI for a different component → unverified (no partial match).
    ctx_other = {
        "viewport_failure_screenshot_uri": (
            f"gs://my-bucket/{viewport_object_name(project_id, 'OtherComponent', width)}"
        )
    }
    result_other = resolve_visual_evidence(project_id, component, width, ctx_other)
    assert result_other["resolved"] is False
    assert result_other["status"] == "unverified"

    # None context → unverified (graceful degradation).
    result_none = resolve_visual_evidence(project_id, component, width, None)
    assert result_none["resolved"] is False
    assert result_none["status"] == "unverified"


# ---------------------------------------------------------------------------
# Salvage bundle payload contract — Python producer ↔ TS consumer seam
# ---------------------------------------------------------------------------


def test_salvage_bundle_payload_contract():
    """_build_salvage_payload must emit exactly the keys handleSalvageBundle reads.

    The TS consumer reads: title, discard_reason, prompt, description, claims,
    related_files (and uses '(untitled)'/'unknown' fallbacks for missing values).
    A producer-side rename would silently degrade consumed bundles while both
    sides' existing unit tests keep passing — this test pins the contract.
    """
    from app.research_completion import _build_salvage_payload

    import uuid

    source_id = uuid.uuid4()
    candidates = [
        {
            "title": "Fix the leaky abstraction",
            "deferred_reason": "scope too large",
            "direction_id": "dir-abc",
            "prompt": "Refactor the storage layer",
            "description": "The storage abstraction leaks implementation details.",
            "related_files": [{"path": "backend/app/storage.py"}],
            "claims": [{"claim": "reduces coupling"}],
            "sources": [],
        },
        {
            # minimal entry — omits optional fields
            "title": None,
            "description": "A minimal finding with no prompt",
        },
    ]

    result = _build_salvage_payload(candidates, source_id)

    # Top-level structure
    assert result["type"] == "salvage_bundle"
    assert result["source_task_id"] == str(source_id)
    assert result["total_discarded"] == 2
    findings = result["salvaged_findings"]
    assert len(findings) == 2

    # Required keys consumed by handleSalvageBundle must be present in every entry
    ts_consumer_keys = {"title", "discard_reason", "prompt", "description", "related_files", "claims"}
    for entry in findings:
        for key in ts_consumer_keys:
            assert key in entry, f"Missing TS-consumer key '{key}' in salvaged_findings entry: {entry}"

    # First entry: rich data round-trips correctly
    f0 = findings[0]
    assert f0["title"] == "Fix the leaky abstraction"
    assert f0["discard_reason"] == "scope too large"
    assert f0["prompt"] == "Refactor the storage layer"
    assert "storage abstraction" in (f0["description"] or "")
    assert f0["related_files"] == [{"path": "backend/app/storage.py"}]
    assert f0["claims"] == [{"claim": "reduces coupling"}]

    # Second entry: None title falls back to description[:80]; missing deferred_reason → "unknown"
    f1 = findings[1]
    assert f1["title"] == "A minimal finding with no prompt"[:80]
    assert f1["discard_reason"] == "unknown"
    assert f1["prompt"] is None  # absent in input → None in output (TS falls back gracefully)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"related_files": ["admin-ui/src/Foo.viewport.test.tsx"]}, True),
        ({"acceptance_evidence": {"kind": "playwright"}}, True),
        ({"related_files": ["backend/testing/test_foo.py"]}, False),
    ],
)
def test_salvage_bundle_surfaces_browser_routing_requirement(candidate, expected):
    """Viewport proof must become a dispatcher-readable payload capability."""

    from app.research_completion import _build_salvage_payload

    payload = _build_salvage_payload([candidate], "source-task")

    assert payload["requires_browser"] is expected
