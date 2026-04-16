"""Regression tests for Claude rate-limit detection.

These tests pin the EXACT verbatim strings that Claude Code / Anthropic API
produce when a real rate limit is hit, and pin the common false-positive
strings (non-Claude "credit"/"billing"/"429"/"overloaded"/"quota") that must
NOT be classified as RATE_LIMIT.

History: the previous RATE_LIMIT_KEYWORDS list matched substrings like
"credit", "billing", "429", "quota exceeded", and "overloaded" anywhere in
the error text. That caused accounts to get disabled for GCP disk quota,
GitHub 429s, Anthropic 529 overloads, credit-card setup errors, and any
message mentioning "billing". See RATE_LIMIT_PATTERNS in max_plan_pool.py.
"""
from unittest.mock import MagicMock

import pytest

from app.failure_classification import (
    INFRA_CLASSES,
    classify_failure,
    normalize_agent_failure_category,
    worst_child_failure_category,
)
from app.max_plan_pool import RATE_LIMIT_PATTERNS
from app.models import Task


def _task(msg: str) -> Task:
    t = MagicMock(spec=Task)
    t.latest_status_message = msg
    t.context = {}
    return t


# ── Positive: real Claude rate-limit messages ──────────────────────────────

VERBATIM_POSITIVES = [
    # Subscription / Max-plan quota messages (Claude Code CLI)
    "You've hit your limit · resets 5am (Asia/Seoul)",
    "You've hit your limit · resets 11:30pm (America/Los_Angeles)",
    "You've hit your usage limit - View usage",
    "Claude AI usage limit reached, please try again after 2pm",
    "You're out of extra usage · resets 11:30am (Asia/Colombo)",
    "5-hour limit reached - resets 9pm",
    # Anthropic API 429
    "API Error: Rate limit reached",
    "This request would exceed your account's rate limit. Please try again later.",
]


@pytest.mark.parametrize("msg", VERBATIM_POSITIVES)
def test_verbatim_claude_rate_limit_messages_classify_as_rate_limit(msg):
    assert classify_failure(_task(msg)) == "RATE_LIMIT", (
        f"Expected RATE_LIMIT for verbatim Claude message: {msg!r}"
    )


def test_pattern_list_is_all_lowercase():
    """Patterns must be lowercase — classify_failure lowercases the input before matching."""
    for p in RATE_LIMIT_PATTERNS:
        assert p == p.lower(), f"Pattern must be lowercase: {p!r}"


def test_pattern_list_has_no_bare_keywords():
    """Guard against regressing to broad keywords that caused false positives."""
    forbidden_bare = {"credit", "billing", "429", "quota exceeded", "overloaded",
                      "too many requests", "rate limit", "usage limit"}
    # A bare forbidden term as a whole pattern (not a substring of a larger phrase)
    # would bring back the false-positive classification.
    for p in RATE_LIMIT_PATTERNS:
        assert p not in forbidden_bare, (
            f"{p!r} is too broad — must be part of a larger Claude-specific phrase"
        )


# ── Negative: false positives from the old broad-keyword classifier ───────

FALSE_POSITIVE_CASES = [
    # Generic billing/credit wording — not Claude rate limits.
    ("credit card declined during checkout", "CODE_ERROR"),
    ("please update your billing address", "CODE_ERROR"),
    ("Bad credentials returned from GitHub", "EXTERNAL_API"),
    # Anthropic 529 overload — server-side capacity, must not disable accounts.
    ("Error: Overloaded", "EXTERNAL_API"),
    ("API is temporarily overloaded, please retry", "EXTERNAL_API"),
    # GitHub 429s — external API issue, not Claude account exhaustion.
    ("GitHub API rate limit exceeded", "EXTERNAL_API"),
    ("api.github.com returned 429 too many requests", "EXTERNAL_API"),
    # Generic HTTP 429 from a downstream service.
    ("HTTP 429 from upstream service", "CODE_ERROR"),
    ("received 429 too many requests from internal service", "CODE_ERROR"),
    # GCP compute/disk quota — provision failure, not rate limit.
    ("CPUS_ALL_REGIONS quota exceeded", "PROVISION_FAILURE"),
    ("disk quota exceeded on /var", "PROVISION_FAILURE"),
    # "exceeded your" appearing in non-rate-limit contexts.
    ("exceeded your PR review limit", "CODE_ERROR"),
    # "plan limit" in unrelated contexts.
    ("hit plan limit for GitHub Actions minutes", "CODE_ERROR"),
    # Token limit — generally means context window, not rate limit.
    ("token limit exceeded for this conversation", "EXTERNAL_API"),
]


@pytest.mark.parametrize("msg,expected", FALSE_POSITIVE_CASES)
def test_non_claude_rate_limit_messages_do_not_classify_as_rate_limit(msg, expected):
    actual = classify_failure(_task(msg))
    assert actual != "RATE_LIMIT", (
        f"{msg!r} classified as RATE_LIMIT — would falsely disable the Claude account. "
        f"Expected something else (typically {expected})."
    )


def test_agent_reported_rate_limited_category_bypasses_text_match():
    """If the agent POSTs a structured failure_category=rate_limited, honor it
    regardless of the status message text — the agent has direct stderr access."""
    t = MagicMock(spec=Task)
    t.latest_status_message = "some generic message"
    t.context = {"failure_category": "rate_limited"}
    assert classify_failure(t) == "RATE_LIMIT"


def test_case_insensitive_matching():
    """Patterns are lowercased; input gets lowercased before comparison."""
    assert classify_failure(_task("API ERROR: RATE LIMIT REACHED")) == "RATE_LIMIT"
    assert classify_failure(_task("you've HIT your LIMIT · resets 5am")) == "RATE_LIMIT"


def test_logs_field_is_searched():
    """classify_failure also scans the logs parameter, not just status message."""
    t = _task("")
    assert classify_failure(t, logs="trace: API Error: Rate limit reached\n") == "RATE_LIMIT"


# ── normalize_agent_failure_category ───────────────────────────────────────

def test_normalize_agent_category_maps_lowercase_rate_limited_to_uppercase():
    """Agent posts lowercase 'rate_limited' — must persist as canonical RATE_LIMIT."""
    t = _task("")
    assert normalize_agent_failure_category("rate_limited", t) == "RATE_LIMIT"


def test_normalize_agent_category_maps_claude_code_failed_to_code_error():
    t = _task("")
    assert normalize_agent_failure_category("claude_code_failed", t) == "CODE_ERROR"


def test_normalize_agent_category_maps_clone_failed_to_provision_failure():
    t = _task("")
    assert normalize_agent_failure_category("clone_failed", t) == "PROVISION_FAILURE"


def test_normalize_agent_category_accepts_already_canonical_names():
    """If the caller already hands us an uppercase class name, pass it through."""
    t = _task("")
    for canonical in ["PROVISION_FAILURE", "RATE_LIMIT", "AGENT_TIMEOUT", "EXTERNAL_API", "CODE_ERROR"]:
        assert normalize_agent_failure_category(canonical, t) == canonical


def test_normalize_agent_category_unknown_label_falls_back_to_classify():
    """Unknown labels must not silently pass through — they should re-classify via text match."""
    t = _task("API Error: Rate limit reached")
    assert normalize_agent_failure_category("some_unknown_thing", t) == "RATE_LIMIT"


def test_normalize_agent_category_none_falls_back_to_classify():
    t = _task("failed to provision VM")
    assert normalize_agent_failure_category(None, t) == "PROVISION_FAILURE"


# ── Provision phrase match (not bare substring) ────────────────────────────

def test_since_provisioning_is_not_provision_failure():
    """Reaper status like 'No heartbeat received after 300s since provisioning'
    must classify as AGENT_TIMEOUT (via 'no heartbeat'), not PROVISION_FAILURE."""
    t = _task("No heartbeat received after 300s since provisioning")
    assert classify_failure(t) == "AGENT_TIMEOUT"


def test_failed_to_provision_still_matches():
    """Existing 'failed to provision VM' path must keep classifying as PROVISION_FAILURE."""
    assert classify_failure(_task("failed to provision VM")) == "PROVISION_FAILURE"
    assert classify_failure(_task("Provisioning failed for zone us-central1")) == "PROVISION_FAILURE"


def test_infra_classes_includes_external_api():
    """Guard against regression: research_completion and direction_outcomes must
    agree on which classes count as infra. EXTERNAL_API must be in the unified set."""
    assert "EXTERNAL_API" in INFRA_CLASSES
    assert "PROVISION_FAILURE" in INFRA_CLASSES
    assert "RATE_LIMIT" in INFRA_CLASSES
    assert "AGENT_TIMEOUT" in INFRA_CLASSES
    assert "CODE_ERROR" not in INFRA_CLASSES


# ── Cascade cancellation classifier ───────────────────────────────────────────

def test_cascade_dependency_task_failed_is_agent_timeout():
    """'Cancelled: dependency task failed' (set on directly-cancelled parent task)
    must classify as AGENT_TIMEOUT — it's an orchestrator decision, not a code defect."""
    assert classify_failure(_task("Cancelled: dependency task failed")) == "AGENT_TIMEOUT"


def test_cascade_parent_dependency_failed_is_agent_timeout():
    """'Cancelled: parent dependency failed' (set on children) must classify as AGENT_TIMEOUT."""
    assert classify_failure(_task("Cancelled: parent dependency failed")) == "AGENT_TIMEOUT"


def test_cascade_parent_evaluation_timeout_is_agent_timeout():
    """Existing 'cancelled: parent evaluation timeout' pattern still matches after broadening."""
    assert classify_failure(_task("Cancelled: parent evaluation timeout")) == "AGENT_TIMEOUT"


def test_cancelled_prefix_broad_match():
    """Any message starting with 'cancelled:' is treated as AGENT_TIMEOUT."""
    assert classify_failure(_task("Cancelled: some new orchestrator reason")) == "AGENT_TIMEOUT"


# ── worst_child_failure_category ───────────────────────────────────────────────

def _child(failure_category=None):
    """Make a minimal Task-like mock with optional failure_category in context."""
    t = MagicMock(spec=Task)
    t.context = {"failure_category": failure_category} if failure_category else {}
    return t


def test_worst_child_failure_category_empty_list_returns_code_error():
    """Empty child list falls back to CODE_ERROR (safest default)."""
    assert worst_child_failure_category([]) == "CODE_ERROR"


def test_worst_child_failure_category_single_code_error():
    assert worst_child_failure_category([_child("CODE_ERROR")]) == "CODE_ERROR"


def test_worst_child_failure_category_single_agent_timeout():
    assert worst_child_failure_category([_child("AGENT_TIMEOUT")]) == "AGENT_TIMEOUT"


def test_worst_child_failure_category_single_provision_failure():
    assert worst_child_failure_category([_child("PROVISION_FAILURE")]) == "PROVISION_FAILURE"


def test_worst_child_failure_category_code_error_wins_over_agent_timeout():
    """CODE_ERROR severity (3) > AGENT_TIMEOUT (2) — CODE_ERROR must win."""
    children = [_child("AGENT_TIMEOUT"), _child("CODE_ERROR")]
    assert worst_child_failure_category(children) == "CODE_ERROR"


def test_worst_child_failure_category_agent_timeout_wins_over_provision_failure():
    """AGENT_TIMEOUT severity (2) > PROVISION_FAILURE (1) — AGENT_TIMEOUT must win."""
    children = [_child("PROVISION_FAILURE"), _child("AGENT_TIMEOUT")]
    assert worst_child_failure_category(children) == "AGENT_TIMEOUT"


def test_worst_child_failure_category_agent_timeout_wins_over_external_api():
    """AGENT_TIMEOUT severity (2) > EXTERNAL_API (1) — AGENT_TIMEOUT must win."""
    children = [_child("EXTERNAL_API"), _child("AGENT_TIMEOUT")]
    assert worst_child_failure_category(children) == "AGENT_TIMEOUT"


def test_worst_child_failure_category_code_error_wins_over_all_infra():
    """CODE_ERROR beats all infra classes."""
    children = [
        _child("PROVISION_FAILURE"),
        _child("RATE_LIMIT"),
        _child("EXTERNAL_API"),
        _child("AGENT_TIMEOUT"),
        _child("CODE_ERROR"),
    ]
    assert worst_child_failure_category(children) == "CODE_ERROR"


def test_worst_child_failure_category_unclassified_child_treated_as_code_error():
    """A child with no failure_category is treated as CODE_ERROR — worst case."""
    children = [_child("AGENT_TIMEOUT"), _child(None)]  # None → treated as CODE_ERROR
    assert worst_child_failure_category(children) == "CODE_ERROR"


def test_worst_child_failure_category_unknown_category_treated_as_code_error():
    """An unrecognised category string is treated as CODE_ERROR to be safe."""
    children = [_child("AGENT_TIMEOUT"), _child("SOME_FUTURE_CATEGORY")]
    assert worst_child_failure_category(children) == "CODE_ERROR"


def test_worst_child_failure_category_all_infra_returns_infra():
    """When all children are infra failures, any infra class may be returned."""
    children = [_child("PROVISION_FAILURE"), _child("RATE_LIMIT")]
    result = worst_child_failure_category(children)
    assert result in ("PROVISION_FAILURE", "RATE_LIMIT")
