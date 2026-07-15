"""Unified audience routing registry for operator-directed messages.

Defines the four-audience taxonomy and a single ``route_event`` resolver
that both the strategic message ledger (``notification_dispatch.post_event_message``)
and the alert/task-notification channel (``alert_policy``) consult.

Fail-closed: any event_type not present in the registry resolves to
``STRATEGIST_INTERNAL``, so a brand-new emitter that forgets to declare
its audience is invisible to the operator until someone consciously
registers it as ``operator-*``.
"""

from __future__ import annotations

# ── Audience constants ──────────────────────────────────────────────────

OPERATOR_ACTION_NEEDED = "operator-action-needed"
"""Blocks the operator or needs their decision/input."""

OPERATOR_FYI = "operator-fyi"
"""Changes something the operator sees or uses; no action required."""

STRATEGIST_INTERNAL = "strategist-internal"
"""Agent / pipeline bookkeeping visible only on the strategist surface."""

PIPELINE_LOG = "pipeline-log"
"""Pure telemetry — logs or observability only."""

# ── Registry ────────────────────────────────────────────────────────────

# NOTE: Seeded with every known event type.  The source of truth for
# ledger-NOTE event types is ``backend/testing/event_type_snapshot.json``.
# Alert event types are seeded from ``alert_policy.ALERT_EVENT_POLICIES``.
#
# Keep this dict sorted by (ledger, alert) groups for maintainability.
_EVENT_AUDIENCE_REGISTRY: dict[str, str] = {
    # === Ledger NOTE event types (from event_type_snapshot.json) =========
    # Routine bookkeeping → strategist-internal
    "pr_merged": STRATEGIST_INTERNAL,
    "direction_milestone": STRATEGIST_INTERNAL,
    "cycle_summary": STRATEGIST_INTERNAL,
    "account_model_regression_quarantined": STRATEGIST_INTERNAL,
    "account_model_regression_recovered": STRATEGIST_INTERNAL,
    "auto_fix_spawned": STRATEGIST_INTERNAL,
    "auto_fix_result": STRATEGIST_INTERNAL,
    "residual_fix_spawned": STRATEGIST_INTERNAL,
    "residual_fix_result": STRATEGIST_INTERNAL,
    "residual_fix_unsuspended": STRATEGIST_INTERNAL,
    "residual_case_auto_resolved": STRATEGIST_INTERNAL,
    "issue_implementation_spawned": STRATEGIST_INTERNAL,
    "review_feedback": STRATEGIST_INTERNAL,
    "feedback_acknowledgment": STRATEGIST_INTERNAL,
    # Escalation → operator-action-needed
    "auto_fix_escalated": OPERATOR_ACTION_NEEDED,
    "residual_fix_escalated": OPERATOR_ACTION_NEEDED,
    "residual_fix_audit_deferred": OPERATOR_ACTION_NEEDED,
    "remediation_escalated": OPERATOR_ACTION_NEEDED,
    "direction_burn_pause": OPERATOR_ACTION_NEEDED,
    "direction_outcome_scaffolding_escalation": OPERATOR_ACTION_NEEDED,
    "direction_churn_pause": OPERATOR_ACTION_NEEDED,
    # High-signal → operator-fyi
    "failure_alert": OPERATOR_FYI,
    "rollback_recommended": OPERATOR_FYI,
    "fix_loop_divergence_regressed": OPERATOR_FYI,
    "release_calcifying": OPERATOR_FYI,
    "release_build_stalled": OPERATOR_FYI,
    "release_stabilize_stalled": OPERATOR_FYI,
    "release_ci_unresolved_stall": OPERATOR_FYI,
    "direction_outcome_stalled": OPERATOR_FYI,
    "direction_staged_activated": OPERATOR_FYI,
    "promotion_readiness_reached": OPERATOR_FYI,
    "release_auto_merge_stalled": OPERATOR_FYI,
    "strategic_steering_deferred": OPERATOR_FYI,
    "strategic_steering_acted": OPERATOR_FYI,
    "capability_trial_stalled": OPERATOR_FYI,
    "capability_verdict_decided": OPERATOR_FYI,
    "capability_trial_ambiguous_lift_escalation": OPERATOR_FYI,
    "question_answer": OPERATOR_FYI,
    # Capability-proof gate auto-graduation — an autonomous gate-mode change.
    # off→observe is informational (nothing blocks yet); →enforce begins
    # BLOCKING task spawning and release promotion, so it needs attention.
    "capability_proof_gate_graduated_observe": OPERATOR_FYI,
    "capability_proof_gate_graduated_enforce": OPERATOR_ACTION_NEEDED,
    # Criterion mutation removes/replaces a quality gate → operator-action-needed
    "criterion_retracted": OPERATOR_ACTION_NEEDED,
    "criterion_superseded": OPERATOR_ACTION_NEEDED,
    # === Alert event types (from alert_policy.ALERT_EVENT_POLICIES) =======
    # Core portable alerts — operator-facing
    "task.failed": OPERATOR_FYI,
    "task.blocked": OPERATOR_FYI,
    "approval.needed": OPERATOR_ACTION_NEEDED,
    "cost.ceiling_reached": OPERATOR_ACTION_NEEDED,
    "cost.spend_hold": OPERATOR_ACTION_NEEDED,
    "capacity.quota_exhausted": OPERATOR_ACTION_NEEDED,
    "capacity.all_accounts_blocked": OPERATOR_ACTION_NEEDED,
    # Self-hosted fleet runner health incidents: operator-owned hardware needs
    # operator remediation; idle GPU capacity is advisory only.
    "runner.deactivated": OPERATOR_ACTION_NEEDED,
    "runner.degraded": OPERATOR_FYI,
    "runner.low_disk": OPERATOR_ACTION_NEEDED,
    "runner.low_memory": OPERATOR_ACTION_NEEDED,
    "runner.idle_gpu": OPERATOR_FYI,
    "runner.token_stale": OPERATOR_ACTION_NEEDED,
    "runner.fleet_no_healthy": OPERATOR_ACTION_NEEDED,
    "runner.post_update_silent": OPERATOR_ACTION_NEEDED,
    "runner.cross_project_access": OPERATOR_ACTION_NEEDED,
    # GCE task-VM proactive resource-health alerts (warm-pool monitoring parity):
    # surfaced to the operator so an OOM- or disk-bound VM is visible in the fleet
    # health summary BEFORE the VM is killed, not only via teardown forensics.
    "vm.low_disk": OPERATOR_FYI,
    "vm.low_memory": OPERATOR_FYI,
    "deployment.halted": OPERATOR_ACTION_NEEDED,
    "deployment.rollback_recommended": OPERATOR_ACTION_NEEDED,
    "deployment.rollback_detected": OPERATOR_ACTION_NEEDED,
    "deployment.auto_pause": OPERATOR_FYI,
    "deployment.forced_resume": OPERATOR_FYI,
    "deployment.flapping_resume": OPERATOR_FYI,
    "deployment.auto_resume": OPERATOR_FYI,
    "deployment.halted_recovered": OPERATOR_FYI,
    "branch.unsafe_config": OPERATOR_ACTION_NEEDED,
    "branch.missing_config": OPERATOR_ACTION_NEEDED,
    "branch.promotion_target_landing": OPERATOR_FYI,
    "merge.unverified": OPERATOR_ACTION_NEEDED,
    "review.escalated": OPERATOR_ACTION_NEEDED,
    "review.fail_open": OPERATOR_ACTION_NEEDED,
    # Release lifecycle
    "release.calcifying": OPERATOR_FYI,
    "release.ci_unresolved": OPERATOR_FYI,
    "release.build_stalled": OPERATOR_FYI,
    "release.stabilize_stalled": OPERATOR_FYI,
    "release.promotion_deploy_failed": OPERATOR_ACTION_NEEDED,
    "release.promotion_auto_merge_stalled": OPERATOR_ACTION_NEEDED,
    "release.managed_check_wait_stalled": OPERATOR_FYI,
    "release.qualification_timeout": OPERATOR_ACTION_NEEDED,
    "release.qualification_crash_loop": OPERATOR_ACTION_NEEDED,
    # Self-review quality
    "sr.catch_rate_below_target": OPERATOR_FYI,
    "sr.lone_checker": OPERATOR_FYI,
    "sr.signal_stale": OPERATOR_FYI,
    # Warm pool
    "warm_pool.payoff": STRATEGIST_INTERNAL,
    "warm_pool.cost_guard": STRATEGIST_INTERNAL,
    # Research directions
    "research.stale_staged_direction": STRATEGIST_INTERNAL,
    "research.direction_auto_completed": STRATEGIST_INTERNAL,
    "research.strategic_issue": OPERATOR_FYI,
    # Rollup / LTS
    "rollup.backmerge_blocked": OPERATOR_ACTION_NEEDED,
    "rollup.pr_blocked": OPERATOR_ACTION_NEEDED,
    # Migrations
    "migration.slot_contention": STRATEGIST_INTERNAL,
    "migration.chain_blocked": STRATEGIST_INTERNAL,
    # Self-improvement
    "self_improvement.payload_too_large": STRATEGIST_INTERNAL,
    # Cost / efficiency
    "cost.unpriced_models": OPERATOR_FYI,
    "efficiency.self_repair_burn_share": STRATEGIST_INTERNAL,
    "efficiency.provider_routing_verdict": STRATEGIST_INTERNAL,
    # === Back-merge / hotfix alerts (no existing policy entry) ============
    "hotfix_backmerge.size_violation": STRATEGIST_INTERNAL,
    # Resident strategist self-observability. A chronically-low operator-decision
    # capture rate means operator corrections are silently failing to reach the
    # durable decisions register. This is internal strategist bookkeeping — the
    # strategist acts on it (re-emit the marker) but it must NEVER reach the
    # operator chat or the alert channel.
    "operator_decision.capture_rate_low": STRATEGIST_INTERNAL,
}

# Set of audiences that reach the operator through normal channels.
_AUDIENCES_THAT_REACH_OPERATOR: frozenset[str] = frozenset(
    {OPERATOR_ACTION_NEEDED, OPERATOR_FYI}
)


# ── Public API ──────────────────────────────────────────────────────────


def route_event(event_type: str, *, message_type: str | None = None) -> str:
    """Resolve the audience for *event_type*.

    Fail-closed: returns ``STRATEGIST_INTERNAL`` for any event_type that
    is not registered — a new emitter that forgets to declare its audience
    is invisible to the operator.

    The ``message_type`` parameter is reserved for future use (e.g.,
    distinguishing NOTE from BLOCKER for the same event_type).
    """
    normalized = (event_type or "").strip()
    if not normalized:
        return STRATEGIST_INTERNAL
    return _EVENT_AUDIENCE_REGISTRY.get(normalized, STRATEGIST_INTERNAL)


def is_operator_visible(audience: str) -> bool:
    """Return True when *audience* is operator-action-needed or operator-fyi."""
    return audience in _AUDIENCES_THAT_REACH_OPERATOR


def stamp_message_audience(
    context: dict | None,
    event_type: str,
    *,
    message_type: str | None = None,
) -> dict:
    """Return a copy of *context* with a canonical ``audience`` stamp.

    This is the shared primitive every operator-reaching emitter routes
    through so that no ``ProjectMessage``/``Alert`` row leaves the process
    without a fail-closed audience classification.  The ledger choke-point
    (:func:`app.notification_dispatch.post_event_message`) stamps inline via
    :func:`route_event`; the directly-constructed BLOCKER/system escalation
    rows call this helper so they carry the *same* stamp instead of bypassing
    the gate.

    The returned dict is a shallow copy (the caller's dict is never mutated).
    ``event_type`` is preserved on the context (set if absent) and the
    ``audience`` key is always overwritten with the canonical resolution — a
    caller-supplied ``audience`` never wins over the registry.
    """
    ctx: dict = dict(context or {})
    ctx.setdefault("event_type", event_type)
    ctx["audience"] = route_event(event_type, message_type=message_type)
    return ctx


def get_registered_event_types() -> frozenset[str]:
    """Return every event_type currently in the registry (testing hook)."""
    return frozenset(_EVENT_AUDIENCE_REGISTRY)


# ── External-webhook namespace ──────────────────────────────────────────
#
# The external notification webhook (``notification_dispatch.dispatch_external_notification``)
# is a THIRD operator-reaching channel, alongside the strategic message ledger
# and the alert/task-notification channel.  Its event identifiers are an uppercase
# namespace (``PR_MERGED``, ``DIRECTION_MILESTONE``, ``RUNNER_DEACTIVATED``) that is
# distinct from the lowercase/dotted registry keys above, so it needs its own
# name→audience table.  This table shares the same four-audience taxonomy and the
# same ``is_operator_visible`` policy as the ledger and alert channels, so all three
# operator-reaching channels now obey one fail-closed gate.
#
# The universe mirrored here is the operator-selectable ``_VALID_NOTIFICATION_EVENTS``
# set validated in ``admin_settings`` — the only events that can be present in
# ``notification_config.enabled_events`` and therefore the only events that can be
# POSTed externally.  Anything not listed fails closed to ``STRATEGIST_INTERNAL`` and
# is dropped, so a strategist-internal/pipeline-log event never leaks even when a
# (legacy) config has it enabled.
_WEBHOOK_EVENT_AUDIENCE: dict[str, str] = {
    # --- Operator-facing terminal / decision events — keep delivering. ---
    "TASK_FAILED": OPERATOR_FYI,
    "TASK_COMPLETED": OPERATOR_FYI,
    "APPROVAL_NEEDED": OPERATOR_ACTION_NEEDED,
    "DEPLOY_HEALTH_ALERT": OPERATOR_FYI,
    "BUDGET_EXCEEDED": OPERATOR_ACTION_NEEDED,
    "AUTO_FIX_ESCALATED": OPERATOR_ACTION_NEEDED,
    "REMEDIATION_ESCALATED": OPERATOR_ACTION_NEEDED,
    "RESIDUAL_FIX_ESCALATED": OPERATOR_ACTION_NEEDED,
    "RESIDUAL_FIX_AUDIT_DEFERRED": OPERATOR_ACTION_NEEDED,
    "DIRECTION_BURN_PAUSE": OPERATOR_ACTION_NEEDED,
    "MERGE_UNVERIFIED": OPERATOR_ACTION_NEEDED,
    "SPEND_HOLD_CREATED": OPERATOR_ACTION_NEEDED,
    "SPEND_HOLD_EXPIRING": OPERATOR_ACTION_NEEDED,
    # --- Runner-health fleet incidents — operator-actionable outages. ---
    "RUNNER_DEACTIVATED": OPERATOR_ACTION_NEEDED,
    "RUNNER_DEGRADED": OPERATOR_FYI,
    "RUNNER_DEGRADED_ESCALATED": OPERATOR_ACTION_NEEDED,
    "RUNNER_FLEET_OFFLINE": OPERATOR_ACTION_NEEDED,
    "RUNNER_LOW_DISK": OPERATOR_FYI,
    "RUNNER_OUTAGE_ESCALATED": OPERATOR_ACTION_NEEDED,
    "RUNNER_LOCAL_HOLD": OPERATOR_ACTION_NEEDED,
    "RUNNER_CONTAINER_HOLD": OPERATOR_ACTION_NEEDED,
    # --- Strategist-internal bookkeeping — MUST NOT leak to the external
    # webhook, even when a legacy notification_config has enabled them. ---
    "PR_MERGED": STRATEGIST_INTERNAL,
    "DIRECTION_MILESTONE": STRATEGIST_INTERNAL,
    "AUTO_FIX_SPAWNED": STRATEGIST_INTERNAL,
    "AUTO_FIX_MERGED": STRATEGIST_INTERNAL,
    "RESIDUAL_FIX_SPAWNED": STRATEGIST_INTERNAL,
    "RESIDUAL_FIX_MERGED": STRATEGIST_INTERNAL,
    "RESIDUAL_FIX_UNSUSPENDED": STRATEGIST_INTERNAL,
}


def route_external_webhook_event(webhook_event_type: str) -> str:
    """Resolve the audience for an external-webhook *webhook_event_type*.

    Fail-closed: returns ``STRATEGIST_INTERNAL`` for any webhook event name that
    is not registered in :data:`_WEBHOOK_EVENT_AUDIENCE`, so an unmapped or
    strategist-internal event never reaches the external webhook.
    """
    normalized = (webhook_event_type or "").strip()
    if not normalized:
        return STRATEGIST_INTERNAL
    return _WEBHOOK_EVENT_AUDIENCE.get(normalized, STRATEGIST_INTERNAL)


def is_external_webhook_operator_visible(webhook_event_type: str) -> bool:
    """Return True when the external-webhook *webhook_event_type* is operator-visible.

    Operator-visible means the resolved audience is ``operator-action-needed`` or
    ``operator-fyi``; strategist-internal / pipeline-log / unmapped events return
    False and are gated out of the external webhook.
    """
    return is_operator_visible(route_external_webhook_event(webhook_event_type))


def get_operator_visible_webhook_events() -> frozenset[str]:
    """Return every webhook event name that resolves operator-visible (testing hook)."""
    return frozenset(
        name
        for name, audience in _WEBHOOK_EVENT_AUDIENCE.items()
        if is_operator_visible(audience)
    )
