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
    "You've hit your session limit · resets 7:10am (UTC)",
    "You've hit your weekly limit · resets 9pm (UTC)",
    "Claude AI usage limit reached, please try again after 2pm",
    "You're out of extra usage · resets 11:30am (Asia/Colombo)",
    "5-hour limit reached - resets 9pm",
    # Weekly (7-day) plan cap — plain-text form the CLI emits alongside the
    # structured seven_day event. Regression guard: these previously fell
    # through to UNKNOWN, so the account was never disabled and the task
    # terminal-FAILED instead of being held + rerouted.
    "You've hit your weekly limit · resets 9pm (UTC)",
    "Weekly limit reached",
    # Anthropic API 429
    "API Error: Rate limit reached",
    "This request would exceed your account's rate limit. Please try again later.",
    # Org monthly spend cap (Claude Code CLI banner). Uses curly apostrophes
    # (’) verbatim — the pattern matches on the apostrophe-free tail
    # "monthly spend limit" so quote style can't break detection.
    "You’ve hit your org’s monthly spend limit · ask your admin to raise it at claude.ai/settings/usage",
    "You've hit your org's monthly spend limit · ask your admin to raise it at claude.ai/settings/usage",
]


@pytest.mark.parametrize("msg", VERBATIM_POSITIVES)
def test_verbatim_claude_rate_limit_messages_classify_as_rate_limit(msg):
    assert classify_failure(_task(msg)) == "RATE_LIMIT", (
        f"Expected RATE_LIMIT for verbatim Claude message: {msg!r}"
    )


@pytest.mark.parametrize(
    "msg",
    [
        "You've hit your weekly limit · resets 9pm (UTC)",
        "Weekly limit reached",
    ],
)
def test_weekly_limit_routes_to_quota_exhausted_kind(msg):
    """A weekly (7-day) cap is a clock-resolving quota wall, so it must classify
    with kind='quota_exhausted' → disable_account_model applies the flat 24h
    cooldown rather than the 5-hour exponential backoff (which would re-cycle the
    task against a still-exhausted account every ~4h)."""
    from app.providers.anthropic import AnthropicProvider
    from app.providers.base import LLMAuthMode

    sig = AnthropicProvider().classify_rate_limit_signal(LLMAuthMode.OAUTH, msg.lower())
    assert sig is not None, (
        f"weekly-limit message not recognized as rate limit: {msg!r}"
    )
    assert sig.kind == "quota_exhausted", (
        f"weekly-limit message should be quota_exhausted (24h cooldown), got {sig.kind!r}"
    )


def test_monthly_spend_limit_routes_to_quota_exhausted_kind():
    """An org monthly spend cap is a billing wall — it doesn't self-reset until
    the billing month rolls over or an admin raises it, so it must classify as
    kind='quota_exhausted' (flat 24h cooldown), not the 5-hour exponential
    that would re-cycle tasks against a still-capped org every few hours."""
    from app.providers.anthropic import AnthropicProvider
    from app.providers.base import LLMAuthMode

    msg = (
        "You’ve hit your org’s monthly spend limit · "
        "ask your admin to raise it at claude.ai/settings/usage"
    )
    sig = AnthropicProvider().classify_rate_limit_signal(LLMAuthMode.OAUTH, msg.lower())
    assert sig is not None, "monthly spend-limit banner not recognized as rate limit"
    assert sig.kind == "quota_exhausted", (
        f"spend-limit banner should be quota_exhausted (24h cooldown), got {sig.kind!r}"
    )


def test_five_hour_limit_stays_short_cooldown_kind():
    """Guard the boundary: a 5-hour throttle must NOT be quota_exhausted — it
    resolves on the short exponential backoff, not a flat 24h hold."""
    from app.providers.anthropic import AnthropicProvider
    from app.providers.base import LLMAuthMode

    sig = AnthropicProvider().classify_rate_limit_signal(
        LLMAuthMode.OAUTH, "5-hour limit reached - resets 9pm"
    )
    assert sig is not None
    assert sig.kind == "rate_limit"


def test_pattern_list_is_all_lowercase():
    """Patterns must be lowercase — classify_failure lowercases the input before matching."""
    for p in RATE_LIMIT_PATTERNS:
        assert p == p.lower(), f"Pattern must be lowercase: {p!r}"


def test_pattern_list_has_no_bare_keywords():
    """Guard against regressing to broad keywords that caused false positives."""
    forbidden_bare = {
        "credit",
        "billing",
        "429",
        "quota exceeded",
        "overloaded",
        "too many requests",
        "rate limit",
        "usage limit",
    }
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


def test_empty_status_and_logs_classify_as_agent_timeout():
    """Whitespace-only combined log → AGENT_TIMEOUT (agent crash), not CODE_ERROR."""
    t = MagicMock(spec=Task)
    t.latest_status_message = ""
    t.context = {}
    assert classify_failure(t) == "AGENT_TIMEOUT"
    assert classify_failure(t, logs="   \n\t") == "AGENT_TIMEOUT"


def test_superseded_message_classifies_as_admin_cancel():
    """'Superseded by fix on PR #N: ...' is system cancellation → ADMIN_CANCEL."""
    msg = "Superseded by fix on PR #1234: review was scoped to a pre-conflict snapshot"
    assert classify_failure(_task(msg)) == "ADMIN_CANCEL"


def test_real_code_error_still_classifies_as_code_error():
    """Regression: typical code-defect messages still classify as CODE_ERROR."""
    assert classify_failure(_task("AssertionError: expected 3, got 4")) == "CODE_ERROR"
    assert (
        classify_failure(_task("TypeError: unsupported operand type(s)"))
        == "CODE_ERROR"
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
    assert (
        classify_failure(t, logs="trace: API Error: Rate limit reached\n")
        == "RATE_LIMIT"
    )


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
    for canonical in [
        "PROVISION_FAILURE",
        "RATE_LIMIT",
        "AGENT_TIMEOUT",
        "EXTERNAL_API",
        "CODE_ERROR",
    ]:
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
    assert (
        classify_failure(_task("Provisioning failed for zone us-central1"))
        == "PROVISION_FAILURE"
    )


def test_infra_classes_includes_external_api():
    """Guard against regression: research_completion and direction_outcomes must
    agree on which classes count as infra. EXTERNAL_API must be in the unified set."""
    assert "EXTERNAL_API" in INFRA_CLASSES
    assert "PROVISION_FAILURE" in INFRA_CLASSES
    assert "RATE_LIMIT" in INFRA_CLASSES
    assert "AGENT_TIMEOUT" in INFRA_CLASSES
    assert "SCOPE_OVERFLOW" in INFRA_CLASSES
    assert "CODE_ERROR" not in INFRA_CLASSES


def test_normalize_agent_category_scope_overflow_maps_to_scope_overflow():
    """Agent posts lowercase 'scope_overflow' — must persist as canonical SCOPE_OVERFLOW
    and NOT fall through to CODE_ERROR (which would penalize direction health)."""
    t = _task("")
    assert normalize_agent_failure_category("scope_overflow", t) == "SCOPE_OVERFLOW"


def test_normalize_agent_category_timeout_maps_to_agent_timeout():
    """Agent posts lowercase 'timeout' when the 45-minute kill timer fires —
    must persist as AGENT_TIMEOUT so scope-downgrade retry logic triggers
    instead of CODE_ERROR's fix-tests/lint guidance."""
    t = _task("")
    assert normalize_agent_failure_category("timeout", t) == "AGENT_TIMEOUT"


def test_classify_failure_respects_scope_overflow_agent_category():
    """A task with ctx['failure_category'] = 'SCOPE_OVERFLOW' must classify as SCOPE_OVERFLOW
    regardless of the status message text."""
    t = _task("some arbitrary error")
    t.context = {"failure_category": "SCOPE_OVERFLOW"}
    assert classify_failure(t) == "SCOPE_OVERFLOW"


# ── Cascade cancellation classifier ───────────────────────────────────────────


def test_cascade_dependency_task_failed_is_admin_cancel():
    """'Cancelled: dependency task failed' is an orchestrator decision → ADMIN_CANCEL."""
    assert (
        classify_failure(_task("Cancelled: dependency task failed")) == "ADMIN_CANCEL"
    )


def test_cascade_parent_dependency_failed_is_admin_cancel():
    """'Cancelled: parent dependency failed' (set on children) → ADMIN_CANCEL."""
    assert (
        classify_failure(_task("Cancelled: parent dependency failed"))
        == "ADMIN_CANCEL"
    )


def test_cascade_parent_evaluation_timeout_is_admin_cancel():
    """'Cancelled: parent evaluation timeout' routes to ADMIN_CANCEL."""
    assert (
        classify_failure(_task("Cancelled: parent evaluation timeout"))
        == "ADMIN_CANCEL"
    )


def test_cancelled_prefix_broad_match():
    """Any message starting with 'cancelled:' is treated as ADMIN_CANCEL."""
    assert (
        classify_failure(_task("Cancelled: some new orchestrator reason"))
        == "ADMIN_CANCEL"
    )


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
    assert (
        worst_child_failure_category([_child("PROVISION_FAILURE")])
        == "PROVISION_FAILURE"
    )


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


# ── classify_failure_taxonomy ──────────────────────────────────────────────

from app.failure_classification import (  # noqa: E402
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
    classify_failure_taxonomy,
)


def _task_with_ctx(msg: str, ctx: dict) -> Task:
    t = MagicMock(spec=Task)
    t.latest_status_message = msg
    t.context = ctx
    return t


def test_taxonomy_review_quality_from_test_failure():
    t = _task_with_ctx("pytest failed: AssertionError in test_foo", {"scope": "small"})
    assert classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY


def test_taxonomy_review_quality_from_lint_failure():
    t = _task_with_ctx("ruff lint failed with 3 errors", {"scope": "small"})
    assert classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY


def test_taxonomy_medium_scope_without_evidence_is_code_defect():
    """Medium-scope CODE_ERROR without scope evidence must classify as code_defect.
    Bare scope='medium'/'large' is not positive evidence of a scope-related failure."""
    t = _task_with_ctx("TypeError: unsupported operand", {"scope": "medium"})
    assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT


def test_taxonomy_scope_related_from_expansion_ratio():
    """expansion_ratio > 1.5 is positive evidence that the scope was exceeded."""
    t = _task_with_ctx(
        "NameError: foo not defined", {"scope": "small", "expansion_ratio": 2.0}
    )
    assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED


def test_taxonomy_expansion_ratio_at_or_below_threshold_is_code_defect():
    """expansion_ratio <= 1.5 is not enough evidence — falls through to code_defect."""
    t = _task_with_ctx(
        "NameError: foo not defined", {"scope": "small", "expansion_ratio": 1.5}
    )
    assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT


def test_taxonomy_calibrated_from_without_scope_evidence_is_code_defect():
    """scope_calibrated_from alone is NOT evidence of a scope-related failure —
    a calibrated task can fail for unrelated code defects (e.g. NameError).
    Without scope keywords or an expansion_ratio signal, fall through to code_defect."""
    t = _task_with_ctx(
        "NameError: foo not defined",
        {"scope": "small", "scope_calibrated_from": "medium"},
    )
    assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT

    # With corroborating expansion_ratio > 1.5, classifies as scope_related.
    t2 = _task_with_ctx(
        "NameError: foo not defined",
        {"scope": "small", "scope_calibrated_from": "medium", "expansion_ratio": 2.0},
    )
    assert classify_failure_taxonomy(t2) == TAXONOMY_SCOPE_RELATED

    # With corroborating scope-keyword in status message, classifies as scope_related.
    t3 = _task_with_ctx(
        "aborted: too many files changed",
        {"scope": "small", "scope_calibrated_from": "medium"},
    )
    assert classify_failure_taxonomy(t3) == TAXONOMY_SCOPE_RELATED


def test_taxonomy_scope_related_from_complexity_message():
    t = _task_with_ctx("aborted: too many files changed", {"scope": "small"})
    assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED


def test_taxonomy_code_defect_default():
    t = _task_with_ctx("AssertionError: expected 3, got 4", {"scope": "small"})
    assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT


def test_taxonomy_infra_for_rate_limit():
    t = _task_with_ctx("API Error: Rate limit reached", {})
    assert classify_failure_taxonomy(t) == TAXONOMY_INFRA_RELATED


def test_taxonomy_infra_for_provision_failure():
    t = _task_with_ctx("failed to provision VM", {})
    assert classify_failure_taxonomy(t) == TAXONOMY_INFRA_RELATED


def test_taxonomy_review_quality_beats_scope():
    """Test/lint signal takes precedence over scope signals — review quality is the root cause.
    Uses real scope evidence (scope_calibrated_from + expansion_ratio) to prove precedence,
    since a bare medium/large scope is no longer treated as evidence."""
    t = _task_with_ctx(
        "pytest failure in test_x",
        {"scope": "large", "scope_calibrated_from": "small", "expansion_ratio": 2.5},
    )
    assert classify_failure_taxonomy(t) == TAXONOMY_REVIEW_QUALITY


# ── Cross-function: normalize_agent_failure_category + classify_failure_taxonomy ──


def test_taxonomy_calibrated_from_with_generic_message_is_code_defect():
    """scope_calibrated_from in ctx alone (no scope keywords, no expansion_ratio)
    is insufficient evidence — falls through to code_defect."""
    for msg in ("generic failure", "AttributeError: 'NoneType' has no attribute 'x'"):
        t = _task_with_ctx(msg, {"scope": "small", "scope_calibrated_from": "medium"})
        assert classify_failure_taxonomy(t) == TAXONOMY_CODE_DEFECT, (
            f"Expected CODE_DEFECT for calibrated-from task with msg={msg!r}"
        )


def test_taxonomy_scope_related_for_agent_reported_scope_overflow():
    """End-to-end: normalize_agent_failure_category('scope_overflow', task) persists
    SCOPE_OVERFLOW in context; classify_failure_taxonomy must then return SCOPE_RELATED
    (SCOPE_OVERFLOW stays in INFRA_CLASSES for retry/budget routing, but the taxonomy
    carve-out routes it to scope_related since the code was not at fault)."""
    t = _task_with_ctx("agent aborted", {})
    normalized = normalize_agent_failure_category("scope_overflow", t)
    assert normalized == "SCOPE_OVERFLOW"
    t.context = {"failure_category": normalized}
    assert classify_failure_taxonomy(t) == TAXONOMY_SCOPE_RELATED


def test_taxonomy_infra_related_for_agent_reported_rate_limited():
    """End-to-end: normalize_agent_failure_category('rate_limited', task) yields
    RATE_LIMIT; classify_failure_taxonomy must then return INFRA_RELATED."""
    t = _task_with_ctx("claude cli exited 1", {})
    normalized = normalize_agent_failure_category("rate_limited", t)
    assert normalized == "RATE_LIMIT"
    t.context = {"failure_category": normalized}
    assert classify_failure_taxonomy(t) == TAXONOMY_INFRA_RELATED


# ── parse_failed + rate-limit correlation ─────────────────────────────────────


def test_normalize_agent_category_parse_failed_ignores_other_attempt_cooldown(
    monkeypatch,
):
    """An account cooldown from another attempt cannot rewrite parse failure."""
    t = _task("")
    assert normalize_agent_failure_category("parse_failed", t) == "CODE_ERROR"


def test_normalize_agent_category_parse_failed_without_rate_limit_is_code_error(
    monkeypatch,
):
    """parse_failed with no active rate-limit window must remain CODE_ERROR —
    a genuine malformed-output failure should not be silently reclassified."""
    t = _task("")
    assert normalize_agent_failure_category("parse_failed", t) == "CODE_ERROR"


# ── db_account_was_rate_limited: durable DB-state check ──────────────────────


@pytest.mark.asyncio
async def test_db_account_was_rate_limited_active_cooldown(monkeypatch):
    """Returns True when DB record shows an active cooldown window."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, MagicMock
    from app.max_plan_pool import db_account_was_rate_limited
    from app.models import MaxPlanAccount

    disabled_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    fake_account = MaxPlanAccount(
        user_id="test",
        name="test-acct",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": disabled_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = fake_account
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_factory = MagicMock(return_value=mock_session)

    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", mock_session_factory)

    result = await db_account_was_rate_limited("some-acct-id")
    assert result is True


@pytest.mark.asyncio
async def test_db_account_was_rate_limited_expired_cooldown(monkeypatch):
    """Returns False when DB record shows an expired cooldown window."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, MagicMock
    from app.max_plan_pool import db_account_was_rate_limited
    from app.models import MaxPlanAccount

    # hits=1 → 30-minute exponential; disabled 2 hours ago → expired
    disabled_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    fake_account = MaxPlanAccount(
        user_id="test",
        name="test-acct",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": disabled_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = fake_account
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_factory = MagicMock(return_value=mock_session)

    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", mock_session_factory)

    result = await db_account_was_rate_limited("some-acct-id")
    assert result is False


@pytest.mark.asyncio
async def test_db_account_was_rate_limited_at_time_respected(monkeypatch):
    """at_time shifts the reference point: same DB record returns True inside
    the window and False after it, using the explicit at_time parameter."""
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock
    from app.max_plan_pool import db_account_was_rate_limited
    from app.models import MaxPlanAccount

    disabled_at = datetime(2026, 1, 1, 12, 0, 0)
    fake_account = MaxPlanAccount(
        user_id="test",
        name="test-acct",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": disabled_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )

    def _make_factory():
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_account
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_session)

    # 10 minutes after disable → inside 30-minute cooldown
    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", _make_factory())
    assert (
        await db_account_was_rate_limited(
            "acct", at_time=disabled_at + timedelta(minutes=10)
        )
        is True
    )

    # 35 minutes after disable → cooldown expired
    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", _make_factory())
    assert (
        await db_account_was_rate_limited(
            "acct", at_time=disabled_at + timedelta(minutes=35)
        )
        is False
    )


@pytest.mark.asyncio
async def test_db_account_was_rate_limited_account_not_found(monkeypatch):
    """Returns False when no account row exists in the DB."""
    from unittest.mock import AsyncMock, MagicMock
    from app.max_plan_pool import db_account_was_rate_limited

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_factory = MagicMock(return_value=mock_session)

    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", mock_session_factory)

    result = await db_account_was_rate_limited("nonexistent-id")
    assert result is False


@pytest.mark.asyncio
async def test_db_account_was_rate_limited_db_error_degrades_to_false(monkeypatch):
    """Returns False (never raises) when the DB session raises an exception."""
    from unittest.mock import AsyncMock, MagicMock
    from app.max_plan_pool import db_account_was_rate_limited

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_factory = MagicMock(return_value=mock_session)

    monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", mock_session_factory)

    result = await db_account_was_rate_limited("acct-id")
    assert result is False


# ── db_pool_wide_rate_limit_cooldown: mixed-state pool classification ────────


def _mock_db_pool(monkeypatch, accounts):
    """Patch AsyncSessionLocal so db_pool_wide_rate_limit_cooldown sees `accounts`."""
    from unittest.mock import AsyncMock, MagicMock

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = accounts
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.max_plan_pool.AsyncSessionLocal", MagicMock(return_value=mock_session)
    )


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_mixed_rate_limited_and_credentialless(monkeypatch):
    """Mixed pool — one account rate-limited, another missing credentials — must
    still be classified as RATE_LIMIT blocked. Mirrors the in-memory classifier:
    if no enabled account is potentially usable AND at least one has an active
    rate-limit entry, the failure is a rate-limit, not an AGENT_TIMEOUT."""
    from datetime import datetime, timedelta, timezone
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import MaxPlanAccount

    rl_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    rl_acct = MaxPlanAccount(
        user_id="u",
        name="rl",
        oauth_token="tok-rl",  # has credentials
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": rl_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )
    no_creds_acct = MaxPlanAccount(
        user_id="u",
        name="no-creds",
        oauth_token=None,  # missing runtime credentials
        rate_limit_state=None,
    )
    _mock_db_pool(monkeypatch, [rl_acct, no_creds_acct])

    assert await db_pool_wide_rate_limit_cooldown() is True


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_returns_false_when_account_potentially_usable(
    monkeypatch,
):
    """A healthy enabled account with credentials and no rate-limit entries means
    the pool is NOT pool-wide rate-limited — even if a sibling account is in
    active cooldown. The healthy account can pick up the next task."""
    from datetime import datetime, timedelta, timezone
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import MaxPlanAccount

    rl_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    rl_acct = MaxPlanAccount(
        user_id="u",
        name="rl",
        oauth_token="tok-rl",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": rl_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )
    healthy_acct = MaxPlanAccount(
        user_id="u",
        name="healthy",
        oauth_token="tok-healthy",
        rate_limit_state=None,
    )
    _mock_db_pool(monkeypatch, [rl_acct, healthy_acct])

    assert await db_pool_wide_rate_limit_cooldown() is False


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_treats_zai_secret_blob_as_runtime_ready(
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount

    rl_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    rl_acct = MaxPlanAccount(
        user_id="u",
        name="rl",
        oauth_token="tok-rl",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": rl_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )
    healthy_zai = MaxPlanAccount(
        user_id="u",
        name="zai",
        provider=LLMProviderName.ZAI,
        auth_mode=LLMAuthMode.API_KEY,
        secret_blob_ref="projects/test/secrets/zai/versions/1",
        rate_limit_state=None,
    )
    _mock_db_pool(monkeypatch, [rl_acct, healthy_zai])

    assert await db_pool_wide_rate_limit_cooldown(providers=["zai"]) is False


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_treats_deepseek_secret_blob_as_runtime_ready(
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount

    rl_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    rl_acct = MaxPlanAccount(
        user_id="u",
        name="rl",
        oauth_token="tok-rl",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": rl_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )
    healthy_deepseek = MaxPlanAccount(
        user_id="u",
        name="deepseek",
        provider=LLMProviderName.DEEPSEEK,
        auth_mode=LLMAuthMode.API_KEY,
        secret_blob_ref="projects/test/secrets/deepseek/versions/1",
        rate_limit_state=None,
    )
    _mock_db_pool(monkeypatch, [rl_acct, healthy_deepseek])

    assert await db_pool_wide_rate_limit_cooldown(providers=["deepseek"]) is False


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_returns_false_when_all_accounts_credentialless(
    monkeypatch,
):
    """Pool with no usable accounts and no rate-limit entries is NOT a rate-limit
    cooldown — it's a misconfiguration. Must return False so the failure path
    doesn't burn retry budget as RATE_LIMIT when the real cause is missing creds."""
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import MaxPlanAccount

    a = MaxPlanAccount(user_id="u", name="a", oauth_token=None, rate_limit_state=None)
    b = MaxPlanAccount(user_id="u", name="b", oauth_token=None, rate_limit_state=None)
    _mock_db_pool(monkeypatch, [a, b])

    assert await db_pool_wide_rate_limit_cooldown() is False


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_returns_false_when_pool_empty(monkeypatch):
    """No enabled accounts in DB → False, never raises."""
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown

    _mock_db_pool(monkeypatch, [])
    assert await db_pool_wide_rate_limit_cooldown() is False


@pytest.mark.asyncio
async def test_db_pool_wide_cooldown_returns_false_when_only_expired_entries(
    monkeypatch,
):
    """All accounts have stale (expired) rate-limit entries but no active cooldown
    and no usable accounts → the pool is "unavailable" but not actively rate-limited.
    Must return False so the failure isn't misclassified as RATE_LIMIT."""
    from datetime import datetime, timedelta, timezone
    from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
    from app.models import MaxPlanAccount

    # hits=1 → 30-min window; 2h ago → expired
    expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    a = MaxPlanAccount(
        user_id="u",
        name="a",
        oauth_token="tok-a",
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": expired_at.isoformat(),
                    "hits": 1,
                    "kind": "rate_limit",
                }
            }
        },
    )
    _mock_db_pool(monkeypatch, [a])
    assert await db_pool_wide_rate_limit_cooldown() is False
