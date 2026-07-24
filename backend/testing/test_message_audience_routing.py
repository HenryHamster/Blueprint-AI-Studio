"""Routing regression pins for the unified audience architecture.

These tests act as structural drift guards:

1. Non-operator audiences (strategist-internal, pipeline-log) must NEVER
   reach any operator-facing channel — ledger, alert, or external webhook.
2. Known strategist-internal event types (size_breach, strategic_message)
   must stay internal unless overridden by an operator-facing message_type.
3. Strategist-authored BLOCKER / BUG_REPORT / QUESTION with no event_type
   must upgrade to operator-action-needed (item-6 fix).

Named acceptance test (per task declaration):
    test_no_strategist_internal_audience_routes_to_operator_webhook
"""

from __future__ import annotations

from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    OPERATOR_FYI,
    STRATEGIST_INTERNAL,
    PIPELINE_LOG,
    get_non_operator_event_types,
    get_operator_visible_webhook_events,
    is_external_webhook_operator_visible,
    route_event,
    stamp_message_audience,
)
from app.alert_policy import ALERT_EVENT_POLICIES, should_emit_project_alert


# ── Regression pin (a): size_breach stays strategist-internal ───────────


def test_regression_agent_annotation_size_breach_is_strategist_internal():
    """size_breach annotations are internal pipeline bookkeeping — must never reach the operator.

    Regression pin: this was the motivating example from the audience-taxonomy design doc.
    """
    assert route_event("agent_annotation.size_breach") == STRATEGIST_INTERNAL
    # Appears in the non-operator exclusion set used by operator_visible_alert_filter.
    assert "agent_annotation.size_breach" in get_non_operator_event_types()
    # Confirm: non-size_breach annotation remains operator-visible.
    assert route_event("agent_annotation") == OPERATOR_FYI


# ── Regression pin (b): strategic_message without message_type → internal ─


def test_regression_strategic_message_no_message_type_is_strategist_internal():
    """strategic_message with no message_type fails closed to strategist-internal.

    The fail-closed default must be preserved; only an explicit operator-facing
    message_type upgrades the audience.
    """
    assert route_event("strategic_message") == STRATEGIST_INTERNAL
    assert route_event("strategic_message", message_type=None) == STRATEGIST_INTERNAL
    assert route_event("strategic_message", message_type="NOTE") == STRATEGIST_INTERNAL
    assert route_event("strategic_message", message_type="FEEDBACK") == STRATEGIST_INTERNAL


# ── Item-6 fix: strategist BLOCKER/BUG_REPORT/QUESTION upgrades audience ─


def test_create_message_bug_report_without_event_type_stamped_operator_action_needed():
    """stamp_message_audience with BUG_REPORT message_type and no registered event_type
    must resolve to operator-action-needed, not silently bury the escalation."""
    ctx = stamp_message_audience({}, "strategic_message", message_type="BUG_REPORT")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED


def test_create_message_blocker_without_event_type_stamped_operator_action_needed():
    """stamp_message_audience with BLOCKER message_type and no registered event_type
    must resolve to operator-action-needed."""
    ctx = stamp_message_audience({}, "strategic_message", message_type="BLOCKER")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED


def test_create_message_question_without_event_type_stamped_operator_action_needed():
    """stamp_message_audience with QUESTION message_type and no registered event_type
    must resolve to operator-action-needed."""
    ctx = stamp_message_audience({}, "strategic_message", message_type="QUESTION")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED


def test_create_message_note_without_event_type_stays_strategist_internal():
    """NOTE/FEEDBACK are not inherently operator-facing — must stay strategist-internal
    when the event_type also resolves internal (fail-closed preserved)."""
    for mt in ("NOTE", "FEEDBACK", "VISION"):
        ctx = stamp_message_audience({}, "strategic_message", message_type=mt)
        assert ctx["audience"] == STRATEGIST_INTERNAL, f"message_type={mt!r} unexpectedly upgraded"


def test_message_type_fallback_absent_event_type_upgrades_blocker():
    """Absent event_type (None path via stamp_message_audience default sentinel)
    + BUG_REPORT upgrades to operator-action-needed."""
    ctx = stamp_message_audience({}, "strategic_message", message_type="BUG_REPORT")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED


def test_registered_operator_event_type_wins_over_message_type():
    """An event_type already resolving to an operator audience is not overridden
    by message_type — the registry wins when it resolves operator-visible."""
    ctx = stamp_message_audience({}, "auto_fix_escalated", message_type="NOTE")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED

    ctx2 = stamp_message_audience({}, "failure_alert", message_type="NOTE")
    assert ctx2["audience"] == OPERATOR_FYI


# ── Webhook drift guard ─────────────────────────────────────────────────


def test_no_strategist_internal_audience_routes_to_operator_webhook():
    """ACCEPTANCE TEST: no strategist-internal or pipeline-log event may reach
    the external operator webhook.

    Iterates the full _WEBHOOK_EVENT_AUDIENCE registry (via
    is_external_webhook_operator_visible) and asserts that every event whose
    audience is non-operator is correctly gated out by the webhook routing layer.

    Concretely: is_external_webhook_operator_visible returns False for every
    webhook event whose audience is STRATEGIST_INTERNAL or PIPELINE_LOG, which
    means dispatch_external_notification skips them even when a legacy
    notification_config lists them as enabled.
    """
    from app.message_audience import _WEBHOOK_EVENT_AUDIENCE  # type: ignore[attr-defined]

    _NON_OPERATOR = frozenset({STRATEGIST_INTERNAL, PIPELINE_LOG})

    operator_visible = get_operator_visible_webhook_events()
    for event_name, audience in _WEBHOOK_EVENT_AUDIENCE.items():
        should_be_visible = audience not in _NON_OPERATOR
        actual = is_external_webhook_operator_visible(event_name)
        assert actual == should_be_visible, (
            f"Webhook event {event_name!r} has audience {audience!r} but "
            f"is_external_webhook_operator_visible returned {actual} (expected {should_be_visible})"
        )
        if audience in _NON_OPERATOR:
            assert event_name not in operator_visible, (
                f"Webhook event {event_name!r} (audience={audience!r}) appears in "
                "get_operator_visible_webhook_events() but must be gated out"
            )


# ── Alert audience drift guard ──────────────────────────────────────────


def test_no_strategist_internal_alert_event_type_would_pass_audience_gate():
    """All alert event types registered strategist-internal are suppressed by
    should_emit_project_alert's audience gate before they reach the operator."""
    non_operator = get_non_operator_event_types()
    for event_type in ALERT_EVENT_POLICIES:
        if event_type not in non_operator:
            continue
        decision = should_emit_project_alert(None, event_type)
        assert not decision.emit, (
            f"Alert event_type {event_type!r} is in the non-operator set (strategist-internal/"
            f"pipeline-log) but should_emit_project_alert returned emit=True — "
            "the audience gate in alert_policy.should_emit_project_alert must suppress it"
        )
        assert "audience:" in (decision.reason or ""), (
            f"Alert event_type {event_type!r} was suppressed but not with an audience reason "
            f"(got reason={decision.reason!r})"
        )
