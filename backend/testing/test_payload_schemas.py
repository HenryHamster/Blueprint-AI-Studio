"""Tests for backend/app/payload_schemas.py."""
import pytest

from app.payload_schemas import (
    ALL_TASK_TYPES,
    TaskType,
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
    "create", "review", "fix", "plan", "research", "strategic_review",
    "design_doc_update", "security_audit", "qa", "deps", "docs", "migrate",
    "smoketest", "refactor", "debug", "perf", "investigate",
}


def test_task_type_enum_has_all_17_types():
    values = {t.value for t in TaskType}
    assert values == EXPECTED_TYPES


def test_all_task_types_frozenset_matches_enum():
    assert ALL_TASK_TYPES == EXPECTED_TYPES
    assert isinstance(ALL_TASK_TYPES, frozenset)


# ---------------------------------------------------------------------------
# All 17 types pass with correct required fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", [
    "create", "refactor", "plan", "migrate", "investigate",
])
def test_prompt_required_types_pass(task_type):
    result = _valid({"type": task_type, "prompt": "Do the thing"})
    assert result["type"] == task_type
    assert result["prompt"] == "Do the thing"


def test_review_passes():
    result = _valid({"type": "review", "pr_number": 42, "branch": "feature/my-branch"})
    assert result["pr_number"] == 42
    assert result["branch"] == "feature/my-branch"


def test_fix_passes():
    result = _valid({"type": "fix", "prompt": "Fix it", "pr_number": 7, "branch": "main"})
    assert result["prompt"] == "Fix it"
    assert result["pr_number"] == 7


@pytest.mark.parametrize("task_type", [
    "debug", "research", "strategic_review", "design_doc_update",
    "security_audit", "qa", "deps", "docs", "smoketest", "perf",
])
def test_system_populated_types_pass_with_no_extra_fields(task_type):
    result = _valid({"type": task_type})
    assert result["type"] == task_type


# ---------------------------------------------------------------------------
# Missing required fields raise ValueError with type name in message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", [
    "create", "refactor", "plan", "migrate", "investigate",
])
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
    result = _valid({"type": "create", "prompt": "x", "base_branch": "feature/my-branch"})
    assert result["base_branch"] == "feature/my-branch"


def test_review_branch_with_dotdot_rejected():
    msg = _invalid({"type": "review", "pr_number": 1, "branch": "main..evil"})
    assert "review" in msg


def test_review_branch_leading_dash_rejected():
    msg = _invalid({"type": "review", "pr_number": 1, "branch": "-bad"})
    assert "review" in msg


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
    assert type(result) is dict  # noqa: E721


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
# validate_internal_payload — fail-open behaviour
# ---------------------------------------------------------------------------

def test_internal_payload_passes_valid():
    raw = {"type": "create", "prompt": "Do it"}
    result = validate_internal_payload(raw, "test_context")
    assert result["prompt"] == "Do it"


def test_internal_payload_returns_original_on_failure(caplog):
    import logging
    raw = {"type": "create"}  # missing prompt
    with caplog.at_level(logging.WARNING):
        result = validate_internal_payload(raw, "my_context")
    assert result is raw  # original dict returned unchanged
    assert "my_context" in caplog.text


def test_internal_payload_logs_warning_on_failure(caplog):
    import logging
    raw = {"type": "totally_unknown_type"}
    with caplog.at_level(logging.WARNING):
        result = validate_internal_payload(raw, "ctx_label")
    assert result is raw
    assert "ctx_label" in caplog.text


def test_internal_payload_does_not_raise_on_bad_input():
    """Must never raise, even with completely broken input."""
    bad_inputs = [
        {},
        {"type": None},
        {"type": "create"},  # missing prompt
        {"type": "review", "pr_number": -1, "branch": "main"},
    ]
    for raw in bad_inputs:
        result = validate_internal_payload(raw, "safety_check")
        assert result is raw


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
    result = _valid({"type": "fix", "prompt": "Fix it", "pr_number": 1, "branch": "main"})
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
