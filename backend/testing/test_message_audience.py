"""Tests for the unified audience routing registry (message_audience.py).

Covers ``route_event`` registered-mapping correctness, the fail-closed default
(unknown event types → strategist-internal), and that ``post_event_message``
stamps the resolved audience onto every row — operator-visible for operator-*
events and non-operator (strategist-feed) for strategist-internal / fail-closed
events.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import ast
import os
from textwrap import dedent

from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    OPERATOR_FYI,
    STRATEGIST_INTERNAL,
    PIPELINE_LOG,
    get_logical_event_pairs,
    get_non_operator_event_types,
    get_operator_event_types,
    get_operator_visible_webhook_events,
    get_registered_event_types,
    is_external_webhook_operator_visible,
    is_operator_visible,
    operator_visible_alert_filter,
    operator_visible_audience_filter,
    operator_visible_nonactionable_message_filter,
    route_event,
    route_external_webhook_event,
    stamp_message_audience,
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
    assert route_event("resident_reply") == OPERATOR_FYI


def test_route_event_criterion_mutations_maps_to_operator_action_needed():
    """Criterion retraction/supersession removes quality gates — needs operator attention."""
    assert route_event("criterion_retracted") == OPERATOR_ACTION_NEEDED
    assert route_event("criterion_superseded") == OPERATOR_ACTION_NEEDED


def test_route_event_directly_authored_escalations_are_operator_facing():
    """Directly-authored operator escalations (previously unregistered → NULL
    audience, fail-OPEN) now resolve to their intended operator audience so the
    emitter stamp is fail-closed, not read-side NULL fall-through."""
    # BLOCKER / attention escalations need operator action.
    assert route_event("handmerge_pr_preserved") == OPERATOR_ACTION_NEEDED
    assert route_event("unsafe_branch_configuration") == OPERATOR_ACTION_NEEDED
    assert (
        route_event("review_fix_lane_gate_integrity_violation")
        == OPERATOR_ACTION_NEEDED
    )
    # Lane-pilot lifecycle WARNING notices remain operator-visible (fyi).
    for event_type in (
        "lane_killed_exhausted",
        "lane_killed_stranded",
        "lane_checkpoint_recover",
        "lane_budget_near_exhausted",
        "pilot_auto_disabled",
    ):
        assert route_event(event_type) == OPERATOR_FYI, event_type
    # Being operator-visible, none are in the non-operator exclusion set.
    non_operator = get_non_operator_event_types()
    assert "handmerge_pr_preserved" not in non_operator
    assert "unsafe_branch_configuration" not in non_operator
    assert "lane_killed_exhausted" not in non_operator


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


def test_route_event_self_hosted_fleet_health_alert_types_are_operator_visible():
    """Every self-hosted fleet health incident has its intended audience."""
    action_needed = {
        "runner.low_disk",
        "runner.low_memory",
        "runner.token_stale",
        "runner.fleet_no_healthy",
        "runner.post_update_silent",
        "runner.cross_project_access",
    }
    for event_type in action_needed:
        assert route_event(event_type) == OPERATOR_ACTION_NEEDED
    assert route_event("runner.idle_gpu") == OPERATOR_FYI


def test_route_event_size_breach_annotation_is_strategist_internal():
    """size_breach annotations are internal pipeline bookkeeping — must never reach the operator.

    Non-regression: other annotation types (comment/issue/vision) remain OPERATOR_FYI.
    """
    assert route_event("agent_annotation.size_breach") == STRATEGIST_INTERNAL
    # Non-regression: other agent annotations remain operator-visible.
    assert route_event("agent_annotation") == OPERATOR_FYI
    # size_breach must appear in the non-operator exclusion set used by
    # operator_visible_alert_filter, so Alert rows tagged with it are gated out.
    non_operator = get_non_operator_event_types()
    assert "agent_annotation.size_breach" in non_operator
    assert "agent_annotation" not in non_operator


def test_route_event_hotfix_backmerge_size_violation_is_strategist_internal():
    """Module size-overflow alerts are strategist-internal bookkeeping."""
    assert route_event("hotfix_backmerge.size_violation") == STRATEGIST_INTERNAL


def test_route_event_pipeline_internal_bookkeeping_is_strategist_internal():
    """Pipeline-internal bookkeeping event types that must NOT reach the operator badge."""
    # Stale-branch cleanup summary (branch_cleanup.py)
    assert route_event("branch.cleanup_summary") == STRATEGIST_INTERNAL
    # Anomaly auto-adjust / restore (autonomous_research.py)
    assert route_event("research.anomaly_auto_adjusted") == STRATEGIST_INTERNAL
    # Churn-retirement recommended — kept paused, strategist decides (direction_lifecycle.py)
    assert route_event("research.churn_retirement_recommended") == STRATEGIST_INTERNAL
    # These pipeline-bookkeeping types appear in get_non_operator_event_types() and
    # are therefore excluded by operator_visible_alert_filter.
    non_operator = get_non_operator_event_types()
    assert "branch.cleanup_summary" in non_operator
    assert "research.anomaly_auto_adjusted" in non_operator
    assert "research.churn_retirement_recommended" in non_operator


def test_route_event_operator_facing_direction_incidents():
    """Direction incidents that need operator attention are classified accordingly."""
    # Escalation notification delivery failed — ERROR alert, operator must act
    assert route_event("research.escalation_delivery_failed") == OPERATOR_ACTION_NEEDED
    # Direction diagnosis stalled — WARNING alert, manual investigation required
    assert route_event("research.diagnosis_stalled") == OPERATOR_ACTION_NEEDED
    # These must NOT appear in the non-operator exclusion set (they reach the badge)
    non_operator = get_non_operator_event_types()
    assert "research.escalation_delivery_failed" not in non_operator
    assert "research.diagnosis_stalled" not in non_operator


def test_route_event_already_registered_reused_types_retain_audience():
    """Reused existing event types (stamped on new call sites) keep their registered audience."""
    # warm_pool.cost_guard and self_improvement.payload_too_large are already registered;
    # their audience must not have changed.
    assert route_event("warm_pool.cost_guard") == STRATEGIST_INTERNAL
    assert route_event("self_improvement.payload_too_large") == STRATEGIST_INTERNAL
    # cost.ceiling_reached is operator-facing (reused on cost_ceiling.py)
    assert route_event("cost.ceiling_reached") == OPERATOR_ACTION_NEEDED
    # research.direction_auto_completed is internal (reused on direction_lifecycle.py)
    assert route_event("research.direction_auto_completed") == STRATEGIST_INTERNAL


def test_new_internal_event_types_appear_in_non_operator_filter():
    """All new strategist-internal alert event types are in the Alert read-side exclusion set.

    This is the end-to-end proof that operator_visible_alert_filter() will drop
    Alert rows tagged with these event_types from badge counts and previews.
    """
    non_operator = get_non_operator_event_types()
    internal_types = {
        "branch.cleanup_summary",
        "research.anomaly_auto_adjusted",
        "research.churn_retirement_recommended",
    }
    for et in internal_types:
        assert et in non_operator, (
            f"{et!r} is registered STRATEGIST_INTERNAL but absent from "
            "get_non_operator_event_types() — operator_visible_alert_filter will NOT exclude it"
        )


# ── route_event – fail-closed ───────────────────────────────────────────


def test_route_event_unknown_event_type_defaults_to_strategist_internal():
    """Any event_type not in the registry resolves to strategist-internal (fail-closed)."""
    assert route_event("completely.new.event_type") == STRATEGIST_INTERNAL
    assert route_event("") == STRATEGIST_INTERNAL
    assert route_event("   ") == STRATEGIST_INTERNAL
    assert route_event(None) == STRATEGIST_INTERNAL  # type: ignore[arg-type]


def test_route_event_unknown_with_message_type_fallback():
    """NOTE message_type does not upgrade an unknown event_type — only BUG_REPORT/BLOCKER/QUESTION do."""
    assert route_event("unknown.event", message_type="NOTE") == STRATEGIST_INTERNAL


def test_route_event_message_type_fallback_bug_report_upgrades_audience():
    """BUG_REPORT with strategist-internal event_type upgrades to operator-action-needed."""
    assert route_event("strategic_message", message_type="BUG_REPORT") == OPERATOR_ACTION_NEEDED


def test_route_event_message_type_fallback_blocker_upgrades_audience():
    """BLOCKER with strategist-internal event_type upgrades to operator-action-needed."""
    assert route_event("strategic_message", message_type="BLOCKER") == OPERATOR_ACTION_NEEDED


def test_route_event_message_type_fallback_question_upgrades_audience():
    """QUESTION with strategist-internal event_type upgrades to operator-action-needed."""
    assert route_event("strategic_message", message_type="QUESTION") == OPERATOR_ACTION_NEEDED


def test_route_event_message_type_fallback_absent_event_type_upgrades():
    """Absent event_type (None/empty) + BUG_REPORT message_type upgrades to operator-action-needed."""
    assert route_event(None, message_type="BUG_REPORT") == OPERATOR_ACTION_NEEDED  # type: ignore[arg-type]
    assert route_event("", message_type="BUG_REPORT") == OPERATOR_ACTION_NEEDED
    assert route_event("   ", message_type="BUG_REPORT") == OPERATOR_ACTION_NEEDED


def test_route_event_message_type_fallback_note_stays_internal():
    """NOTE/FEEDBACK/VISION message_type does NOT upgrade a strategist-internal event_type."""
    for mt in ("NOTE", "FEEDBACK", "VISION"):
        assert route_event("strategic_message", message_type=mt) == STRATEGIST_INTERNAL, mt
    assert route_event("unknown.event", message_type="FEEDBACK") == STRATEGIST_INTERNAL


def test_route_event_registered_operator_event_not_affected_by_message_type():
    """An event_type already resolving to an operator audience is unchanged by message_type."""
    # auto_fix_escalated is registered OPERATOR_ACTION_NEEDED — message_type must not alter it.
    assert route_event("auto_fix_escalated", message_type="NOTE") == OPERATOR_ACTION_NEEDED
    assert route_event("failure_alert", message_type="NOTE") == OPERATOR_FYI
    # An event_type explicitly registered strategist-internal + NOTE → still internal.
    assert route_event("pr_merged", message_type="NOTE") == STRATEGIST_INTERNAL


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


def test_all_alert_policy_event_types_have_registry_entry_or_explicit_audience():
    """Every key in alert_policy.ALERT_EVENT_POLICIES must be in the audience
    registry or carry an explicit non-null ``policy.audience``.

    Drift direction: a new operator-facing alert added to ALERT_EVENT_POLICIES
    without a registry entry silently fails closed to ``strategist-internal``
    and never reaches the operator.  This test forces a conscious
    operator-visibility decision for every alert event type.
    """
    from app.alert_policy import ALERT_EVENT_POLICIES

    registered = get_registered_event_types()
    missing: list[str] = []
    for event_type, policy in ALERT_EVENT_POLICIES.items():
        if event_type not in registered and policy.audience is None:
            missing.append(event_type)

    assert not missing, (
        "Alert policy event types without a registry entry or explicit audience:\n"
        + "\n".join(f"  - {et}" for et in sorted(missing))
        + "\n\nAdd each to _EVENT_AUDIENCE_REGISTRY in backend/app/message_audience.py "
        "or set an explicit ``audience`` on its AlertEventPolicy in "
        "backend/app/alert_policy.py."
    )


def test_runner_health_alert_event_constants_are_registered():
    """Runner health emitters cannot silently fall through the audience gate.

    ``runner_health_alerts`` declares its event constants at module scope and
    passes them as ``event_type`` to ``create_project_alert_if_allowed``.
    Derive that set from the source rather than duplicating it, so a future
    runner-health alert must receive an explicit audience registration.
    """
    source_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "runner_health_alerts.py"
    )
    with open(source_path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=source_path)

    string_constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    emitted_event_types: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) != "create_project_alert_if_allowed":
            continue
        for keyword in node.keywords:
            if keyword.arg != "event_type":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                emitted_event_types.add(keyword.value.value)
            elif isinstance(keyword.value, ast.Name):
                event_type = string_constants.get(keyword.value.id)
                if event_type is not None:
                    emitted_event_types.add(event_type)

    assert emitted_event_types, "Expected runner health alert emitters"
    missing = emitted_event_types - get_registered_event_types()
    assert not missing, (
        "Runner health alert event types missing from the audience registry: "
        + ", ".join(sorted(missing))
    )


# ── Conversation surface routing drift guards ────────────────────────────


def test_conversation_event_types_surface_drift_guard():
    """Every event type in CONVERSATION_EVENT_TYPES must be in the audience registry.

    CONVERSATION_EVENT_TYPES in ``project_message_creation.py`` controls surface
    routing (CONVERSATION vs ACTIVITY) in ``build_project_message``.  A new
    conversational event type added to CONVERSATION_EVENT_TYPES without a
    corresponding ``_EVENT_AUDIENCE_REGISTRY`` entry will fail this test, forcing
    a conscious audience decision.
    """
    from app.project_message_creation import CONVERSATION_EVENT_TYPES

    registered = get_registered_event_types()
    missing = CONVERSATION_EVENT_TYPES - registered
    assert not missing, (
        "Conversation event types missing from the audience registry:\n"
        + "\n".join(f"  - {et!r}" for et in sorted(missing))
        + "\n\nAdd each to _EVENT_AUDIENCE_REGISTRY in "
        "backend/app/message_audience.py."
    )


def test_operator_visible_undotted_event_types_not_conversational_must_be_explicitly_excluded():
    """Operator-visible undotted event types are either conversational or consciously excluded.

    A new operator-visible (operator-action-needed or operator-fyi) undotted event
    type added to ``_EVENT_AUDIENCE_REGISTRY`` without a ``CONVERSATION_EVENT_TYPES``
    entry or explicit exclusion will silently route to the ACTIVITY surface instead
    of CONVERSATION.  This test ensures every such type is consciously decided.

    Dotted event types (``task.*``, ``runner.*``, etc.) are categorical alert types
    that go through ``Alert`` pathways, not ``build_project_message`` surface routing,
    so they are excluded from this check.
    """
    from app.project_message_creation import CONVERSATION_EVENT_TYPES

    # Operator-visible undotted event types that are intentionally non-conversational:
    # lifecycle notices, pipeline alerts, and direct escalations route to ACTIVITY.
    # MONTHLY_BUDGET_ALERT is an Alert event type (goes through Alert() constructor,
    # not build_project_message), so it routes to the alert surface, not CONVERSATION.
    _NON_CONVERSATIONAL_OPERATOR_LEDGER_TYPES: frozenset[str] = frozenset({
        "handmerge_pr_preserved",
        "unsafe_branch_configuration",
        "review_fix_lane_gate_integrity_violation",
        "repeat_fingerprint_halt",
        "lane_killed_exhausted",
        "lane_killed_stranded",
        "lane_checkpoint_recover",
        "lane_budget_near_exhausted",
        "pilot_auto_disabled",
        "operator_command",
        "strategic_check_in",
        "agent_annotation",
        "MONTHLY_BUDGET_ALERT",
    })

    registered = get_registered_event_types()
    # Focus on undotted NOTE-type event types.  Dotted types are categorical
    # alert types that go through Alert pathways, not build_project_message
    # surface routing.
    operator_undotted = {
        et for et in registered
        if is_operator_visible(route_event(et)) and "." not in et
    }

    covered = CONVERSATION_EVENT_TYPES | _NON_CONVERSATIONAL_OPERATOR_LEDGER_TYPES
    missing = operator_undotted - covered
    assert not missing, (
        "Operator-visible undotted event types not in CONVERSATION_EVENT_TYPES "
        "or the non-conversational allowlist:\n"
        + "\n".join(
            f"  - {et!r} → {route_event(et)}" for et in sorted(missing)
        )
        + "\n\nEither add to CONVERSATION_EVENT_TYPES in "
        "backend/app/project_message_creation.py (conversational) or add to "
        "_NON_CONVERSATIONAL_OPERATOR_LEDGER_TYPES in this test (intentionally "
        "non-conversational)."
    )

    # Sanity-check: every allowlist entry is registered and operator-visible.
    for et in _NON_CONVERSATIONAL_OPERATOR_LEDGER_TYPES:
        assert et in registered, (
            f"Allowlist entry {et!r} is not in the audience registry. "
            "Remove it from the allowlist."
        )
        assert is_operator_visible(route_event(et)), (
            f"Allowlist entry {et!r} is not operator-visible. "
            "Remove it from the allowlist."
        )


# ── Alert read-model audience filter (operator_visible_alert_filter) ─────


def test_non_operator_event_types_derived_from_registry():
    """The precomputed non-operator event_type set is derived from the registry
    and contains explicitly strategist-internal alert types but no operator-* ones."""
    non_operator = get_non_operator_event_types()
    # Explicitly strategist-internal alert event types are present.
    assert "warm_pool.payoff" in non_operator
    assert "hotfix_backmerge.size_violation" in non_operator
    assert "migration.slot_contention" in non_operator
    # Operator-visible alert event types are NOT filtered out.
    assert "task.failed" not in non_operator
    assert "approval.needed" not in non_operator
    assert "runner.deactivated" not in non_operator
    # Every member genuinely resolves to a non-operator audience.
    for event_type in non_operator:
        assert not is_operator_visible(route_event(event_type)), event_type


def _compile_alert_filter(event_types_to_probe=("warm_pool.payoff",)):
    """Compile operator_visible_alert_filter(Alert.event_type) to a literal SQL string."""
    from sqlalchemy.dialects import postgresql

    from app.models import Alert

    clause = operator_visible_alert_filter(Alert.event_type)
    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_operator_visible_alert_filter_is_fail_closed():
    """The Alert filter is fail-closed: only explicitly registered operator-visible
    event_types pass through.  NULL and unregistered event_types are excluded —
    expressed as an ``IN (...)`` allowlist clause with no IS NULL pass-through."""
    sql = _compile_alert_filter()
    # NULL rows are NOT retained — no IS NULL pass-through.
    assert "IS NULL" not in sql
    # Uses an explicit IN allowlist, not a NOT IN denylist.
    assert "NOT IN" not in sql
    assert "IN" in sql
    # Operator-visible alert event types are in the allowlist.
    assert "task.failed" in sql
    assert "approval.needed" in sql
    # Strategist-internal event types are NOT in the allowlist.
    assert "warm_pool.payoff" not in sql
    assert "hotfix_backmerge.size_violation" not in sql


def test_operator_event_types_registry_is_consistent():
    """Operator and non-operator event_type sets are disjoint and cover the full registry."""
    operator_types = get_operator_event_types()
    non_operator_types = get_non_operator_event_types()
    registered = get_registered_event_types()

    # Every registered event_type appears in exactly one set.
    assert operator_types.isdisjoint(non_operator_types), (
        "Operator and non-operator sets must not overlap"
    )
    assert operator_types | non_operator_types == registered, (
        "Every registered event_type must be in exactly one of the two sets"
    )

    # Spot-check: known operator-visible events are in the operator set.
    assert "task.failed" in operator_types
    assert "approval.needed" in operator_types
    assert "runner.deactivated" in operator_types

    # Spot-check: known non-operator events are NOT in the operator set.
    assert "warm_pool.payoff" not in operator_types
    assert "hotfix_backmerge.size_violation" not in operator_types
    assert "migration.slot_contention" not in operator_types

    # Every member genuinely resolves to an operator-visible audience.
    for event_type in operator_types:
        assert is_operator_visible(route_event(event_type)), event_type


# ── External-webhook audience gate ──────────────────────────────────────


def test_route_external_webhook_operator_events_are_visible():
    """Operator-facing webhook events resolve to an operator audience."""
    assert route_external_webhook_event("RUNNER_DEACTIVATED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("BUDGET_EXCEEDED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("APPROVAL_NEEDED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("TASK_FAILED") == OPERATOR_FYI
    assert route_external_webhook_event("RUNNER_DEGRADED") == OPERATOR_FYI


def test_route_external_webhook_internal_events_are_strategist_internal():
    """Strategist-internal webhook events resolve to strategist-internal — never leak."""
    assert route_external_webhook_event("PR_MERGED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("DIRECTION_MILESTONE") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("AUTO_FIX_SPAWNED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("AUTO_FIX_MERGED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("RESIDUAL_FIX_SPAWNED") == STRATEGIST_INTERNAL


def test_route_external_webhook_unmapped_fails_closed():
    """Unmapped / blank webhook event names fail closed to strategist-internal."""
    assert route_external_webhook_event("SOME_UNMAPPED_EVENT") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("   ") == STRATEGIST_INTERNAL
    assert route_external_webhook_event(None) == STRATEGIST_INTERNAL  # type: ignore[arg-type]


def test_is_external_webhook_operator_visible():
    """The webhook gate mirrors is_operator_visible: True for operator-*, False otherwise."""
    assert is_external_webhook_operator_visible("RUNNER_DEACTIVATED") is True
    assert is_external_webhook_operator_visible("AUTO_FIX_ESCALATED") is True
    assert is_external_webhook_operator_visible("TASK_FAILED") is True
    # Strategist-internal + unmapped are gated out.
    assert is_external_webhook_operator_visible("PR_MERGED") is False
    assert is_external_webhook_operator_visible("DIRECTION_MILESTONE") is False
    assert is_external_webhook_operator_visible("SOME_UNMAPPED_EVENT") is False


def test_operator_visible_webhook_events_excludes_internal():
    """The operator-visible webhook set contains operator events and excludes internal ones."""
    visible = get_operator_visible_webhook_events()
    assert "RUNNER_DEACTIVATED" in visible
    assert "AUTO_FIX_ESCALATED" in visible
    assert "PR_MERGED" not in visible
    assert "DIRECTION_MILESTONE" not in visible


def test_logical_event_pairs_have_consistent_audiences():
    """Every logically-paired (internal, webhook) event resolves to the same audience.

    Drift between _EVENT_AUDIENCE_REGISTRY (lowercase/dotted keys) and
    _WEBHOOK_EVENT_AUDIENCE (UPPERCASE keys) for events that represent the
    same logical operation is a silent routing bug.  This test is the
    cross-check that fails CI when a pair diverges, so an edit to one table
    that forgets to update the other is caught before shipping.
    """
    pairs = get_logical_event_pairs()
    assert pairs, "Expected at least one logical event pair to be registered"
    mismatched: list[str] = []
    for internal_key, webhook_key in pairs.items():
        internal_audience = route_event(internal_key)
        webhook_audience = route_external_webhook_event(webhook_key)
        if internal_audience != webhook_audience:
            mismatched.append(
                f"  {internal_key!r} → {internal_audience!r}  vs  "
                f"{webhook_key!r} → {webhook_audience!r}"
            )
    assert not mismatched, (
        "Logical event pairs have inconsistent audience classifications between "
        "_EVENT_AUDIENCE_REGISTRY and _WEBHOOK_EVENT_AUDIENCE:\n"
        + "\n".join(mismatched)
        + "\n\nFix by aligning the audience in one or both registries in "
        "backend/app/message_audience.py, then update _LOGICAL_EVENT_PAIRS if "
        "the events are no longer equivalent."
    )


def test_logical_event_pairs_are_registered_in_their_respective_tables():
    """Every pair entry has both its internal and webhook keys in their registries.

    Catches typos or stale entries in _LOGICAL_EVENT_PAIRS that reference a key
    that was renamed or removed from one of the tables.
    """
    pairs = get_logical_event_pairs()
    internal_registry = get_registered_event_types()
    webhook_keys = _get_all_webhook_event_keys()
    unknown: list[str] = []
    for internal_key, webhook_key in pairs.items():
        if internal_key not in internal_registry:
            unknown.append(f"  internal key {internal_key!r} not in _EVENT_AUDIENCE_REGISTRY")
        if webhook_key not in webhook_keys:
            unknown.append(f"  webhook key {webhook_key!r} not in _WEBHOOK_EVENT_AUDIENCE")
    assert not unknown, (
        "_LOGICAL_EVENT_PAIRS references keys absent from their registry:\n"
        + "\n".join(unknown)
    )


def _get_all_webhook_event_keys() -> set[str]:
    """Return all registered webhook event keys (test-only helper)."""
    from app import message_audience as _ma
    return set(getattr(_ma, "_WEBHOOK_EVENT_AUDIENCE", {}).keys())


def test_valid_notification_events_all_present_in_webhook_registry():
    """Every operator-selectable notification event must have a webhook registry entry.

    ``_VALID_NOTIFICATION_EVENTS`` in ``admin_settings`` is the universe the
    operator can enable; ``_WEBHOOK_EVENT_AUDIENCE`` in ``message_audience`` is the
    routing table that decides whether ``route_external_webhook_event`` delivers or
    silently drops the notification.  If an event is selectable but absent from the
    webhook registry, ``route_external_webhook_event`` fails closed to
    ``STRATEGIST_INTERNAL`` and the operator enables the event but never receives it.

    This guard fails CI the moment an event is added to one list but not the other.
    """
    from app.routers.admin_settings import _VALID_NOTIFICATION_EVENTS

    webhook_keys = _get_all_webhook_event_keys()
    missing = sorted(_VALID_NOTIFICATION_EVENTS - webhook_keys)
    assert not missing, (
        "Operator-selectable notification events are missing from the webhook "
        "audience registry (_WEBHOOK_EVENT_AUDIENCE in message_audience.py).\n"
        "Missing events: " + ", ".join(missing) + "\n\n"
        "A missing entry means route_external_webhook_event() will fail closed to "
        "STRATEGIST_INTERNAL and the notification is silently dropped even when the "
        "operator has enabled it.\n\n"
        "Fix: add each missing event to _WEBHOOK_EVENT_AUDIENCE in "
        "backend/app/message_audience.py with its intended audience classification."
    )


# ── post_event_message audience gating (via notification_dispatch) ──────


@pytest.mark.asyncio
async def test_post_event_message_creates_for_operator_fyi(monkeypatch):
    """post_event_message creates a ProjectMessage for operator-fyi events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

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
async def test_post_event_message_tags_strategist_internal_as_non_operator(monkeypatch):
    """Strategist-internal events create a non-operator strategist-feed row.

    Per the design, ``strategist-internal`` bookkeeping still lands as a row the
    strategist may want, but is stamped ``audience="strategist-internal"`` so it
    is never operator-visible. It is NOT an operator-visible NOTE.
    """
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

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
async def test_post_event_message_unknown_event_fails_closed_to_internal(monkeypatch):
    """Unregistered event types fail closed to strategist-internal (never operator-visible)."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

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
async def test_post_event_message_creates_for_operator_action_needed(monkeypatch):
    """post_event_message creates a ProjectMessage for operator-action-needed events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

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
    mock_session.add = MagicMock()

    msg = await nd.post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="some.telemetry.event",
        summary="telemetry only",
    )
    assert msg is None, "Expected None (no ledger row) for pipeline-log event"
    mock_session.add.assert_not_called()


# ── stamp_message_audience (shared stamping primitive) ──────────────────


def test_stamp_message_audience_stamps_operator_audience():
    """The shared stamp helper writes the canonical operator audience."""
    ctx = stamp_message_audience({"branch": "main"}, "auto_fix_escalated")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED
    assert ctx["event_type"] == "auto_fix_escalated"
    assert ctx["branch"] == "main"  # caller keys preserved


def test_stamp_message_audience_fails_closed_for_unknown_event():
    """An unregistered event_type stamps strategist-internal (fail-closed)."""
    ctx = stamp_message_audience({}, "brand.new.unregistered.event")
    assert ctx["audience"] == STRATEGIST_INTERNAL


def test_stamp_message_audience_does_not_mutate_caller_dict():
    """The helper returns a copy; the caller's dict is untouched."""
    original = {"foo": "bar"}
    result = stamp_message_audience(original, "pr_merged")
    assert "audience" not in original, "caller dict must not be mutated"
    assert result["audience"] == STRATEGIST_INTERNAL
    assert result is not original


def test_stamp_message_audience_registry_wins_over_caller_audience():
    """A caller-supplied audience never overrides the registry resolution."""
    ctx = stamp_message_audience(
        {"audience": OPERATOR_FYI}, "pr_merged"
    )
    assert ctx["audience"] == STRATEGIST_INTERNAL


def test_stamp_message_audience_handles_none_context():
    ctx = stamp_message_audience(None, "direction_burn_pause")
    assert ctx == {
        "event_type": "direction_burn_pause",
        "audience": OPERATOR_ACTION_NEEDED,
    }


# ── Fail-closed structural guard: no operator-reaching emitter bypasses ──
#
# post_event_message stamps the audience inline; the directly-constructed
# BLOCKER escalation rows in notification_dispatch.py must route through the
# same gate (stamp_message_audience) instead of building a raw context dict.
# This AST scan is the structural ratchet: a NEW operator-reaching (system,
# agent, or strategic-authored) ProjectMessage added to notification_dispatch.py
# without an audience stamp in its enclosing function fails CI, closing the
# fail-closed guarantee's hole.

_AUDIENCE_STAMP_CALLS = frozenset({"stamp_message_audience", "route_event"})
_OPERATOR_REACHING_FROM_TYPES: frozenset[str] = frozenset({"system", "agent", "strategic"})

# Both the raw model constructor and the canonical builder produce a
# ``ProjectMessage`` row.  ``build_project_message`` does NOT stamp an audience
# itself (it only resolves surface/handling_state), so every operator-reaching
# call of EITHER must stamp the audience in its enclosing function — otherwise
# the row lands with a NULL audience that the read filter fails OPEN on.
_MESSAGE_BUILDER_CALLS = frozenset({"ProjectMessage", "build_project_message"})


def _ast_called_name(call: ast.Call) -> str | None:
    """Return the bare callee name for ``foo(...)`` or ``mod.foo(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _collect_ungated_emitters(source: str, *, filename: str, rel: str) -> list[str]:
    """Return offender strings for operator-reaching ProjectMessage sites whose
    enclosing function does not call an audience-stamping helper."""

    def _param_defaults(func_node) -> dict[str, ast.expr]:
        """Map each parameter name that has a default to its default AST node.

        Covers positional-or-keyword defaults (tail-aligned to ``args.args``) and
        keyword-only defaults (``kwonlyargs``/``kw_defaults``). Parameters without a
        default are absent from the map.
        """
        fargs = func_node.args
        defaults: dict[str, ast.expr] = {}
        posargs = fargs.args
        if fargs.defaults:
            for name, default in zip(posargs[-len(fargs.defaults):], fargs.defaults):
                defaults[name.arg] = default
        for name, default in zip(fargs.kwonlyargs, fargs.kw_defaults):
            if default is not None:
                defaults[name.arg] = default
        return defaults

    def _is_operator_reaching_message_call(node: ast.Call, enclosing) -> bool:
        """A ProjectMessage(...)/build_project_message(...) construction whose
        ``from_type`` provably reaches the operator surface (system/agent/strategic).

        ``from_type`` is operator-reaching when it is either a literal constant in
        the operator-reaching set, OR a bare parameter reference whose *default*
        value is such a constant. The defaulted-parameter case closes the hole
        where an emitter (e.g. ``post_question_answer(from_type="strategic")``)
        passes ``from_type=from_type`` — a non-literal that the constant-only check
        skipped even though its default reaches the operator. A parameter with no
        resolvable constant default stays skipped (its runtime value is unknown at
        scan time)."""
        if _ast_called_name(node) not in _MESSAGE_BUILDER_CALLS:
            return False
        param_defaults = _param_defaults(enclosing) if enclosing is not None else {}
        for kw in node.keywords:
            if kw.arg != "from_type":
                continue
            val = kw.value
            if isinstance(val, ast.Constant):
                return val.value in _OPERATOR_REACHING_FROM_TYPES
            if isinstance(val, ast.Name):
                default = param_defaults.get(val.id)
                if isinstance(default, ast.Constant):
                    return default.value in _OPERATOR_REACHING_FROM_TYPES
            return False
        return False

    def _stamps_audience(func_node) -> bool:
        return any(
            isinstance(n, ast.Call) and _ast_called_name(n) in _AUDIENCE_STAMP_CALLS
            for n in ast.walk(func_node)
        )

    tree = ast.parse(source, filename=filename)
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) not in _MESSAGE_BUILDER_CALLS:
            continue
        enclosing = None
        for fn in funcs:
            if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                if enclosing is None or fn.lineno > enclosing.lineno:
                    enclosing = fn
        if not _is_operator_reaching_message_call(node, enclosing):
            continue
        func_name = enclosing.name if enclosing is not None else "<module>"
        if enclosing is None or not _stamps_audience(enclosing):
            offenders.append(
                f"  {rel}:{node.lineno} in {func_name}() — operator-reaching ProjectMessage "
                "site not routed through the audience gate (no "
                "stamp_message_audience/route_event call)"
            )
    return offenders


def test_scan_flags_unstamped_system_message():
    """A system ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            ctx = {"event_type": "some_escalation"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="system",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_system_message():
    """A system ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            ctx = {"event_type": "some_escalation"}
            ctx = stamp_message_audience(ctx, "some_escalation")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="system",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_unstamped_agent_message():
    """An agent-authored ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_agent_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="agent",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_agent_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_agent_message():
    """An agent-authored ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_agent_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            ctx = stamp_message_audience(ctx, "some_event")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="agent",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_unstamped_strategic_message():
    """A strategic-authored ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_strategic_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="strategic",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_strategic_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_strategic_message():
    """A strategic-authored ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_strategic_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            ctx = stamp_message_audience(ctx, "some_event")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="strategic",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_user_message():
    """A user-authored ProjectMessage is NOT operator-reaching — it is skipped."""
    source = dedent(
        """
        async def post_user_thing(session, project_id, content):
            ctx = {"event_type": "chat"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="user",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_non_constant_from_type():
    """A ProjectMessage whose from_type is a variable (not a constant) is skipped —
    it cannot be proven operator-reaching at scan time."""
    source = dedent(
        """
        async def post_variable_thing(session, project_id, content, from_type):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_defaulted_operator_reaching_from_type():
    """A ProjectMessage whose from_type is a parameter that DEFAULTS to an
    operator-reaching constant (e.g. "strategic") is flagged when unstamped — the
    default reaches the operator even though the argument is not a literal at the
    construction site."""
    source = dedent(
        """
        async def post_answer(session, project_id, content, from_type="strategic"):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={"event_type": "question_answer"},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:3 in post_answer() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_defaulted_operator_reaching_from_type():
    """The same defaulted-from_type emitter passes once its function stamps."""
    source = dedent(
        """
        async def post_answer(session, project_id, content, from_type="strategic"):
            ctx = stamp_message_audience({"event_type": "question_answer"}, "question_answer")
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_defaulted_user_from_type():
    """A parameter that defaults to "user" is NOT operator-reaching — skipped."""
    source = dedent(
        """
        async def post_user_default(session, project_id, content, from_type="user"):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


# Modules with directly-authored operator-reaching ProjectMessage/build_project_message
# sites.  notification_dispatch.py is the original choke-point; the other modules
# construct their escalation/attention rows via ``build_project_message`` and stamp
# at the emitter so they carry a fail-closed audience.
#
# NOTE: This list is no longer used by test_no_operator_visible_emitter_bypasses_audience_gate,
# which now scans all modules under backend/app/ exhaustively (no allowlist).  We keep
# the variable for reference / legacy test isolation if needed.
_MODULES_WITH_OPERATOR_MESSAGES = [
    "notification_dispatch.py",
    "review_loop_convergence.py",
    "branch_resolution.py",
    "review_fix_lane_pilot.py",
    "review_pipeline.py",
    "research_direction_state.py",
    "capability_trial_verdict.py",
    "strategic_completion.py",
    "work_session_command_execution.py",
    "routers/admin_project_strategic_session.py",
    "routers/admin_agent_ops.py",
]


def test_scan_flags_unstamped_build_project_message():
    """A build_project_message(...) call (the canonical builder) with an
    operator-reaching from_type and no audience stamp is flagged — the builder
    does NOT stamp on its own, so the emitter must."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            return await build_project_message(
                session,
                project_id=project_id,
                from_type="system",
                content=content,
                context={"event_type": "some_escalation"},
            )
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:3 in post_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_build_project_message():
    """A build_project_message(...) call whose function stamps passes."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            ctx = stamp_message_audience({"x": 1}, "some_escalation")
            return await build_project_message(
                session,
                project_id=project_id,
                from_type="system",
                content=content,
                context=ctx,
            )
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_no_operator_visible_emitter_bypasses_audience_gate():
    """Whole-file invariant: every operator-reaching ProjectMessage /
    build_project_message construction under backend/app/ routes its audience
    through the shared gate.

    The test globs the entire app tree so any new module with an unstamped
    operator-reaching ProjectMessage/build_project_message call fails CI
    automatically — no allowlist update required.

    This is the structural ratchet that prevents the operator-visible audience
    leak from silently reoccurring: a freshly added emitter that forgets to call
    ``stamp_message_audience`` (or ``route_event``) in its enclosing function is
    caught by this scan. Fix by stamping the context dict with the appropriate
    event_type before passing it to ``build_project_message`` or the
    ``ProjectMessage`` constructor.
    """
    import glob

    app_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    all_offenders: list[str] = []
    for fpath in sorted(glob.glob(os.path.join(app_dir, "**/*.py"), recursive=True)):
        rel = os.path.relpath(fpath, app_dir)
        with open(fpath, encoding="utf-8") as f:
            source = f.read()
        offenders = _collect_ungated_emitters(
            source, filename=fpath, rel=rel
        )
        all_offenders.extend(offenders)

    assert not all_offenders, (
        "Found operator-reaching ProjectMessage/build_project_message site(s) that "
        "do NOT route their audience through the shared gate "
        "(stamp_message_audience/route_event). Every operator-reaching emitter must "
        "be stamped so a fail-closed audience classification is guaranteed:\n"
        + "\n".join(all_offenders)
    )


def test_no_operator_reaching_emitter_outside_scan_leaks():
    """Exhaustive-glob scan catches every operator-reaching emitter, including
    any module outside the old allowlist.

    The previous allowlist-based scan only checked 11 known modules, so a 12th
    operator-reaching emitter (e.g. strategic_resident_reconciliation.py) could
    silently land rows with a NULL audience that the read filter fails OPEN on.
    This test verifies that:

    1. The exhaustive glob scan covers ``strategic_resident_reconciliation.py``
       (which previously bypassed the gate).
    2. ``"resident_reply"`` is registered in the audience registry as OPERATOR_FYI.
    3. The stamped context in ``_create_resident_reply`` passes the gate scan.
    """
    import glob

    # (1) Verify the event_type is registered in the audience registry.
    assert route_event("resident_reply") == OPERATOR_FYI, (
        "resident_reply must be registered as OPERATOR_FYI"
    )
    non_operator = get_non_operator_event_types()
    assert "resident_reply" not in non_operator, (
        "resident_reply is operator-visible, must not be in the non-operator filter"
    )

    # (2) Verify the glob scan includes strategic_resident_reconciliation.py.
    app_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    all_files = sorted(glob.glob(os.path.join(app_dir, "**/*.py"), recursive=True))
    reconciliation_file = os.path.join(app_dir, "strategic_resident_reconciliation.py")
    assert reconciliation_file in all_files, (
        "strategic_resident_reconciliation.py must be in the glob scan set"
    )

    # (3) Scan that file and confirm no offenders — the stamp closes the hole.
    with open(reconciliation_file, encoding="utf-8") as f:
        source = f.read()
    offenders = _collect_ungated_emitters(
        source,
        filename=reconciliation_file,
        rel="strategic_resident_reconciliation.py",
    )
    assert not offenders, (
        "strategic_resident_reconciliation.py must route its operator-reaching "
        "build_project_message call through the audience gate. "
        "Offenders found:\n" + "\n".join(offenders)
    )


# ── Behavioral: escalation posters stamp an operator-visible audience ────


@pytest.mark.asyncio
async def test_auto_fix_escalation_message_stamps_operator_action_needed(monkeypatch):
    """post_auto_fix_escalation_message stamps operator-action-needed on the row."""
    import app.notification_dispatch as nd

    monkeypatch.setattr(
        nd, "open_system_bug_or_blocker_exists", AsyncMock(return_value=None)
    )
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await nd.post_auto_fix_escalation_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        branch="main",
        failure_count=5,
    )
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    assert msg.context["event_type"] == "auto_fix_escalated"
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_residual_fix_escalation_message_stamps_operator_action_needed(
    monkeypatch,
):
    """post_residual_fix_escalation_message stamps operator-action-needed."""
    import app.notification_dispatch as nd

    monkeypatch.setattr(
        nd, "open_system_bug_or_blocker_exists", AsyncMock(return_value=None)
    )
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await nd.post_residual_fix_escalation_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        fingerprint="abc123",
        failure_count=4,
    )
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    assert msg.context["event_type"] == "residual_fix_escalated"
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_question_answer_stamps_audience_default_from_type(monkeypatch):
    """post_question_answer stamps the registry audience on its context even with
    the default operator-reaching from_type="strategic". Binds the reply's
    visibility to the registry (OPERATOR_FYI today) instead of leaving it an
    unstamped, always-visible row."""
    import uuid as _uuid

    import app.notification_dispatch as nd

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        nd,
        "lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    orig_id = _uuid.uuid4()
    msg = await nd.post_question_answer(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        original_msg_id=orig_id,
        answer="here you go",
    )
    # Default from_type is the operator-reaching "strategic".
    assert msg.from_type == "strategic"
    assert msg.context["event_type"] == "question_answer"
    assert msg.context["audience"] == OPERATOR_FYI
    assert msg.context["reply_to"] == str(orig_id)
    assert msg.sequence == 1
    assert msg.surface == "conversation"
    assert msg.handling_state == "responded"
    assert msg.reply_to_message_id == orig_id
    mock_session.add.assert_called_once()


# ── Structural AST scan: Alert(...) constructors must carry event_type ───
#
# operator_visible_alert_filter(Alert.event_type) is fail-closed (IN-allowlist).
# A directly-constructed Alert(...) without an event_type= keyword has a NULL
# event_type and is silently EXCLUDED from operator-facing surfaces.
# The correct pattern is either:
#   (a) Alert(..., event_type="registered.event") in the constructor call, OR
#   (b) routing through create_project_alert_if_allowed (which stamps it).
# This ratchet prevents ungated Alert constructors from silently regressing.

_ALERT_GATE_CALL = "create_project_alert_if_allowed"


def _collect_ungated_alert_constructors(
    source: str, *, filename: str, rel: str
) -> list[str]:
    """Return offender strings for Alert(...) constructor calls whose enclosing
    function neither supplies an event_type= keyword nor calls
    create_project_alert_if_allowed."""

    tree = ast.parse(source, filename=filename)
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def _enclosing_func(lineno: int):
        best = None
        for fn in funcs:
            if fn.lineno <= lineno <= (fn.end_lineno or fn.lineno):
                if best is None or fn.lineno > best.lineno:
                    best = fn
        return best

    def _has_event_type_keyword(call: ast.Call) -> bool:
        return any(kw.arg == "event_type" for kw in call.keywords)

    def _calls_gate(func_node) -> bool:
        return any(
            isinstance(n, ast.Call) and _ast_called_name(n) == _ALERT_GATE_CALL
            for n in ast.walk(func_node)
        )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) != "Alert":
            continue
        if _has_event_type_keyword(node):
            continue
        enclosing = _enclosing_func(node.lineno)
        if enclosing is not None and _calls_gate(enclosing):
            continue
        func_name = enclosing.name if enclosing is not None else "<module>"
        offenders.append(
            f"  {rel}:{node.lineno} in {func_name}() — Alert() constructed without "
            "event_type= keyword and not routed through create_project_alert_if_allowed"
        )
    return offenders


# ── Unit tests for the Alert scanner ────────────────────────────────────


def test_alert_scan_flags_constructor_without_event_type():
    """An Alert(...) call with no event_type= keyword is flagged."""
    source = dedent(
        """
        def emit_alert(session, project_id):
            alert = Alert(
                project_id=project_id,
                level="WARNING",
                message="something happened",
            )
            session.add(alert)
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert len(offenders) == 1
    assert "emit_alert()" in offenders[0]
    assert "event_type=" in offenders[0]


def test_alert_scan_accepts_constructor_with_event_type():
    """An Alert(...) call that includes event_type= is accepted."""
    source = dedent(
        """
        def emit_alert(session, project_id):
            alert = Alert(
                project_id=project_id,
                level="WARNING",
                message="something happened",
                event_type="branch.cleanup_summary",
            )
            session.add(alert)
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_alert_scan_accepts_function_routing_through_gate():
    """An Alert(...) call without event_type= is accepted when the function also
    calls create_project_alert_if_allowed (it delegates to the gated path)."""
    source = dedent(
        """
        async def emit_alert(session, project_id, condition):
            if condition:
                await create_project_alert_if_allowed(
                    session,
                    project_id=project_id,
                    event_type="task.failed",
                    message="task failed",
                )
            else:
                alert = Alert(
                    project_id=project_id,
                    level="INFO",
                    message="fallback",
                )
                session.add(alert)
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_alert_scan_flags_multiple_ungated_constructors():
    """Multiple Alert(...) calls without event_type= in the same file are all flagged."""
    source = dedent(
        """
        def emit_a(session, project_id):
            alert = Alert(project_id=project_id, message="a")
            session.add(alert)

        def emit_b(session, project_id):
            alert = Alert(project_id=project_id, message="b")
            session.add(alert)
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert len(offenders) == 2
    assert any("emit_a()" in o for o in offenders)
    assert any("emit_b()" in o for o in offenders)


def test_alert_scan_skips_non_alert_calls():
    """Calls to other functions (AlertPolicy, AlertEvent, etc.) are not flagged."""
    source = dedent(
        """
        def configure_policy():
            policy = AlertPolicy(level="WARNING")
            return policy
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_alert_scan_mixed_gated_and_ungated():
    """Only the ungated Alert(...) call is flagged when another in the file has event_type=."""
    source = dedent(
        """
        def emit_gated(session, project_id):
            alert = Alert(project_id=project_id, message="ok", event_type="task.failed")
            session.add(alert)

        def emit_ungated(session, project_id):
            alert = Alert(project_id=project_id, message="bad")
            session.add(alert)
        """
    )
    offenders = _collect_ungated_alert_constructors(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert len(offenders) == 1
    assert "emit_ungated()" in offenders[0]


# ── Structural guard: every Alert event_type must be registered ──────────


def _collect_alert_constructors_with_unregistered_event_type(
    source: str, *, filename: str, rel: str, registered: frozenset[str]
) -> list[str]:
    """Return offender strings for Alert(...) calls whose event_type= literal is
    not present in the audience registry."""
    tree = ast.parse(source, filename=filename)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) != "Alert":
            continue
        for kw in node.keywords:
            if kw.arg != "event_type":
                continue
            if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
                continue
            event_type = kw.value.value
            if event_type not in registered:
                offenders.append(
                    f"  {rel}:{node.lineno} — Alert(event_type={event_type!r}) "
                    "is not registered in _EVENT_AUDIENCE_REGISTRY; "
                    "add it to backend/app/message_audience.py with its intended audience"
                )
    return offenders


def test_no_alert_constructor_uses_unregistered_event_type():
    """Whole-app invariant: every Alert(event_type=...) literal must be present
    in the audience registry.

    operator_visible_alert_filter() is fail-closed (IN-allowlist): an Alert whose
    event_type is not in _EVENT_AUDIENCE_REGISTRY is silently EXCLUDED from the
    operator badge / preview — it will never be visible to the operator.

    This ratchet catches the gap structurally: adding a new Alert() with an
    unregistered event_type now fails CI immediately rather than silently
    disappearing at runtime.  Fix by adding the event_type to _EVENT_AUDIENCE_REGISTRY
    in backend/app/message_audience.py with the correct audience.
    """
    import glob

    app_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    registered = get_registered_event_types()
    all_offenders: list[str] = []
    for fpath in sorted(glob.glob(os.path.join(app_dir, "**/*.py"), recursive=True)):
        rel = os.path.relpath(fpath, app_dir)
        with open(fpath, encoding="utf-8") as f:
            source = f.read()
        all_offenders.extend(
            _collect_alert_constructors_with_unregistered_event_type(
                source, filename=fpath, rel=rel, registered=registered
            )
        )

    assert not all_offenders, (
        "Found Alert() constructor(s) under backend/app/ with an event_type= that "
        "is NOT registered in _EVENT_AUDIENCE_REGISTRY (message_audience.py).\n"
        "operator_visible_alert_filter() is fail-closed (IN-allowlist), so an "
        "unregistered event_type will be EXCLUDED from the operator badge/preview — "
        "the alert will silently disappear from the operator UI.\n"
        "Fix: add each event_type to _EVENT_AUDIENCE_REGISTRY with its intended audience:\n"
        + "\n".join(all_offenders)
    )


# ── Whole-file invariant: zero ungated Alert constructors across all app modules ──


def test_no_alert_constructor_bypasses_event_type_gate():
    """Whole-file invariant: every Alert(...) constructed anywhere under backend/app/
    carries an event_type= keyword (or is in a function that routes through
    create_project_alert_if_allowed).

    The test globs the entire app tree so any new module with an ungated Alert()
    constructor fails CI automatically — no allowlist update required.

    This is the structural ratchet that prevents the operator-badge leak from
    silently reoccurring: operator_visible_alert_filter(Alert.event_type) is now
    fail-closed (IN-allowlist), so any Alert(...) without an event_type= keyword
    will be silently excluded from the operator badge/preview.
    Fix by adding event_type=<registered_key> to the Alert(...) call (register the
    key in message_audience.py if needed).
    """
    import glob

    app_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    all_offenders: list[str] = []
    for fpath in sorted(glob.glob(os.path.join(app_dir, "**/*.py"), recursive=True)):
        rel = os.path.relpath(fpath, app_dir)
        with open(fpath, encoding="utf-8") as f:
            source = f.read()
        offenders = _collect_ungated_alert_constructors(
            source, filename=fpath, rel=rel
        )
        all_offenders.extend(offenders)

    assert not all_offenders, (
        "Found Alert() constructor(s) under backend/app/ that do NOT supply an "
        "event_type= keyword or route through create_project_alert_if_allowed. "
        "operator_visible_alert_filter is fail-closed (IN-allowlist), so an Alert "
        "without an event_type will be silently excluded from the operator badge/preview. "
        "Fix by adding event_type=<registered_key> to the Alert(...) call (register the "
        "key in message_audience.py if needed):\n" + "\n".join(all_offenders)
    )


# ── NULL-audience quarantine for non-actionable messages ────────────────


def test_null_audience_nonactionable_message_excluded_from_operator_counts():
    """NULL-audience NOTE messages are excluded from the operator badge count.

    ``operator_visible_audience_filter`` is the legacy-preserving filter that
    retains NULL-audience rows so pre-registry BLOCKER/QUESTION rows remain
    operator-visible even without an audience stamp.  Non-actionable types
    (NOTE/FEEDBACK/VISION) carry no such implicit operator intent — a NOTE with
    no audience stamp is pre-registry bookkeeping that should be treated as
    strategist-internal, not operator-visible.

    ``operator_visible_nonactionable_message_filter`` is the strict variant: it
    requires an *explicit* operator audience stamp and does NOT retain NULL rows.
    ``get_operator_message_counts`` uses this strict filter for its NOTE count
    and NOTE unread-count queries, so a NULL-audience NOTE no longer inflates
    the operator badge.

    This test compiles both filters to SQL and verifies the structural difference:
    the strict filter has no IS NULL pass-through, while the legacy filter does.
    """
    from sqlalchemy.dialects import postgresql
    from app.models import ProjectMessage

    def _compile(clause) -> str:
        return str(
            clause.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

    legacy_sql = _compile(operator_visible_audience_filter(ProjectMessage.context))
    strict_sql = _compile(operator_visible_nonactionable_message_filter(ProjectMessage.context))

    # The legacy filter retains NULL-audience rows (IS NULL pass-through).
    assert "IS NULL" in legacy_sql, (
        "operator_visible_audience_filter must retain NULL-audience rows (legacy behaviour)"
    )

    # The strict filter does NOT retain NULL-audience rows — no IS NULL clause.
    assert "IS NULL" not in strict_sql, (
        "operator_visible_nonactionable_message_filter must NOT retain NULL-audience rows; "
        "a NULL-audience NOTE is pre-registry bookkeeping and must be excluded from operator counts"
    )

    # The strict filter includes explicit operator audiences.
    assert "operator-action-needed" in strict_sql
    assert "operator-fyi" in strict_sql

    # The strict filter uses an IN clause, not NOT IN — it does not fall back
    # to "anything not explicitly non-operator" (which would retain NULL).
    assert "NOT IN" not in strict_sql, (
        "operator_visible_nonactionable_message_filter must use IN (explicit allowlist), "
        "not NOT IN (exclusion denylist), so NULL-audience rows are not implicitly retained"
    )

    # Verify that stamping a NULL-audience NOTE with an unregistered event_type
    # produces a STRATEGIST_INTERNAL audience — after a quarantine sweep, such
    # rows would be explicitly classified and excluded by both filters.
    stamped_ctx = stamp_message_audience({}, "", message_type="NOTE")
    assert stamped_ctx["audience"] == STRATEGIST_INTERNAL, (
        "A NOTE with no event_type must stamp STRATEGIST_INTERNAL (fail-closed); "
        "once stamped, operator_visible_nonactionable_message_filter excludes it"
    )
