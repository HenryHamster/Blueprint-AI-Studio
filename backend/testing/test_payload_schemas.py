"""Tests for backend/app/payload_schemas.py."""

import pytest

from app.payload_schemas import (
    ALL_TASK_TYPES,
    PayloadTooLargeError,
    TaskType,
    _MAX_PAYLOAD_BYTES,
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
# TaskType enum / ALL_TASK_TYPES
# ---------------------------------------------------------------------------

EXPECTED_TYPES = {
    "create",
    "review",
    "panel_review",
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
    "memo",
    "verify_tests",
    "compute_experiment",
    "web_research",
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
    raw = {"type": "perf", "prompt": "Profile the hot path"}
    result = _valid(raw)
    assert result["prompt"] == "Profile the hot path"


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
