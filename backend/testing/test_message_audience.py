"""Tests for the unified audience routing registry (message_audience.py).

Covers ``route_event`` registered-mapping correctness, the fail-closed default
(unknown event types → strategist-internal), and that ``post_event_message``
stamps the resolved audience onto every row — operator-visible for operator-*
events and non-operator (strategist-feed) for strategist-internal / fail-closed
events.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    OPERATOR_FYI,
    STRATEGIST_INTERNAL,
    PIPELINE_LOG,
    get_registered_event_types,
    is_operator_visible,
    route_event,
)


# ── route_event – known mappings ────────────────────────────────────────


def test_route_event_escalation_maps_to_operator_action_needed():
    """Escalation event types that block or ask the operator."""
    assert route_event("auto_fix_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("residual_fix_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("remediation_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_burn_pause") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_churn_pause") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_outcome_scaffolding_escalation") == OPERATOR_ACTION_NEEDED


def test_route_event_routine_maps_to_strategist_internal():
    """Routine bookkeeping event types."""
    assert route_event("pr_merged") == STRATEGIST_INTERNAL
    assert route_event("cycle_summary") == STRATEGIST_INTERNAL
    assert route_event("direction_milestone") == STRATEGIST_INTERNAL
    assert route_event("auto_fix_spawned") == STRATEGIST_INTERNAL
    assert route_event("review_feedback") == STRATEGIST_INTERNAL


def test_route_event_high_signal_maps_to_operator_fyi():
    """High-signal NOTE event types."""
    assert route_event("failure_alert") == OPERATOR_FYI
    assert route_event("rollback_recommended") == OPERATOR_FYI
    assert route_event("release_calcifying") == OPERATOR_FYI
    assert route_event("promotion_readiness_reached") == OPERATOR_FYI
    assert route_event("strategic_steering_deferred") == OPERATOR_FYI


def test_route_event_criterion_mutations_maps_to_operator_action_needed():
    """Criterion retraction/supersession removes quality gates — needs operator attention."""
    assert route_event("criterion_retracted") == OPERATOR_ACTION_NEEDED
    assert route_event("criterion_superseded") == OPERATOR_ACTION_NEEDED


def test_route_event_capability_proof_gate_graduation_split_by_mode():
    """Gate auto-graduation splits audience by target mode: observe is FYI,
    enforce (which begins blocking spawns/promotion) needs operator attention."""
    assert route_event("capability_proof_gate_graduated_observe") == OPERATOR_FYI
    assert (
        route_event("capability_proof_gate_graduated_enforce")
        == OPERATOR_ACTION_NEEDED
    )


def test_route_event_alert_types_mapped():
    """Alert event types from alert_policy.ALERT_EVENT_POLICIES are registered."""
    assert route_event("task.failed") == OPERATOR_FYI
    assert route_event("approval.needed") == OPERATOR_ACTION_NEEDED
    assert route_event("runner.deactivated") == OPERATOR_ACTION_NEEDED
    assert route_event("warm_pool.payoff") == STRATEGIST_INTERNAL
    assert route_event("migration.slot_contention") == STRATEGIST_INTERNAL


def test_route_event_hotfix_backmerge_size_violation_is_strategist_internal():
    """Module size-overflow alerts are strategist-internal bookkeeping."""
    assert route_event("hotfix_backmerge.size_violation") == STRATEGIST_INTERNAL


# ── route_event – fail-closed ───────────────────────────────────────────


def test_route_event_unknown_event_type_defaults_to_strategist_internal():
    """Any event_type not in the registry resolves to strategist-internal (fail-closed)."""
    assert route_event("completely.new.event_type") == STRATEGIST_INTERNAL
    assert route_event("") == STRATEGIST_INTERNAL
    assert route_event("   ") == STRATEGIST_INTERNAL
    assert route_event(None) == STRATEGIST_INTERNAL  # type: ignore[arg-type]


def test_route_event_unknown_with_message_type_fallback():
    """The message_type parameter is reserved — unknown event_type still defaults to internal."""
    assert route_event("unknown.event", message_type="NOTE") == STRATEGIST_INTERNAL


# ── is_operator_visible ────────────────────────────────────────────────


def test_is_operator_visible_true_for_operator_audiences():
    assert is_operator_visible(OPERATOR_ACTION_NEEDED) is True
    assert is_operator_visible(OPERATOR_FYI) is True


def test_is_operator_visible_false_for_internal_audiences():
    assert is_operator_visible(STRATEGIST_INTERNAL) is False
    assert is_operator_visible(PIPELINE_LOG) is False


def test_is_operator_visible_false_for_unknown_audience():
    assert is_operator_visible("unknown-audience") is False


# ── Registry completeness ───────────────────────────────────────────────


def test_registry_contains_all_known_event_types():
    """Smoke check: the registry is non-empty and contains both ledger and alert types."""
    registered = get_registered_event_types()
    assert len(registered) > 50, f"Expected 50+ entries, got {len(registered)}"
    assert "pr_merged" in registered
    assert "task.failed" in registered
    assert "hotfix_backmerge.size_violation" in registered


# ── post_event_message audience gating (via notification_dispatch) ──────


@pytest.mark.asyncio
async def test_post_event_message_creates_for_operator_fyi():
    """post_event_message creates a ProjectMessage for operator-fyi events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = AsyncMock()

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="failure_alert",
        summary="Test failure alert",
    )
    assert msg is not None, "Expected a ProjectMessage for operator-fyi event"
    assert msg.context["audience"] == OPERATOR_FYI
    # The message was added to the session
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_tags_strategist_internal_as_non_operator():
    """Strategist-internal events create a non-operator strategist-feed row.

    Per the design, ``strategist-internal`` bookkeeping still lands as a row the
    strategist may want, but is stamped ``audience="strategist-internal"`` so it
    is never operator-visible. It is NOT an operator-visible NOTE.
    """
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = AsyncMock()

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="pr_merged",
        summary="PR merged (bookkeeping)",
    )
    assert msg is not None, "Expected a strategist-feed row for strategist-internal event"
    assert msg.context["audience"] == STRATEGIST_INTERNAL
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_unknown_event_fails_closed_to_internal():
    """Unregistered event types fail closed to strategist-internal (never operator-visible)."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = AsyncMock()

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="new_undocumented_event",
        summary="Brand new event",
    )
    assert msg is not None
    assert msg.context["audience"] == STRATEGIST_INTERNAL, (
        "Unregistered event must fail closed to strategist-internal, never operator-visible"
    )
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_creates_for_operator_action_needed():
    """post_event_message creates a ProjectMessage for operator-action-needed events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = AsyncMock()

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="direction_churn_pause",
        summary="Direction churn paused",
    )
    assert msg is not None, "Expected a ProjectMessage for operator-action-needed event"
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    # The message was added to the session
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_skips_row_for_pipeline_log(monkeypatch):
    """Pipeline-log events are pure telemetry — no ledger row is created (returns None).

    No registered event currently maps to ``pipeline-log``, so patch the resolver
    to exercise the skip branch directly.
    """
    import app.notification_dispatch as nd

    monkeypatch.setattr(nd, "route_event", lambda *a, **k: PIPELINE_LOG)

    mock_session = AsyncMock()
    mock_session.add = AsyncMock()

    msg = await nd.post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="some.telemetry.event",
        summary="telemetry only",
    )
    assert msg is None, "Expected None (no ledger row) for pipeline-log event"
    mock_session.add.assert_not_called()
