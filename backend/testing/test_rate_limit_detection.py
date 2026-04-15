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

from app.failure_classification import classify_failure
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
