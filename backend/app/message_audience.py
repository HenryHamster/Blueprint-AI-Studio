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

from typing import Any

from sqlalchemy import or_

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
    "discovery.review_completed": STRATEGIST_INTERNAL,
    "discovery.review_blocked": STRATEGIST_INTERNAL,
    # Exact release-barrier receipts are private Resident input. They advance
    # planning only after qualification/deployment has actually resolved and
    # must not create duplicate operator notifications.
    "release.qualification_resolved": STRATEGIST_INTERNAL,
    "release.serving_confirmation_timed_out": STRATEGIST_INTERNAL,
    "release.serving_confirmed": STRATEGIST_INTERNAL,
    "release.serving_failed": STRATEGIST_INTERNAL,
    # Two-speed Resident handoff packets are private coordination between the
    # Fast Resident and Workday; they must never appear in the operator thread.
    "workday_consultation": STRATEGIST_INTERNAL,
    "workday_consultation_result": STRATEGIST_INTERNAL,
    # Escalation → operator-action-needed
    "auto_fix_escalated": OPERATOR_ACTION_NEEDED,
    "residual_fix_escalated": OPERATOR_ACTION_NEEDED,
    "residual_fix_audit_deferred": OPERATOR_ACTION_NEEDED,
    "remediation_escalated": OPERATOR_ACTION_NEEDED,
    "direction_burn_pause": OPERATOR_ACTION_NEEDED,
    "direction_outcome_scaffolding_escalation": OPERATOR_ACTION_NEEDED,
    "direction_churn_pause": OPERATOR_ACTION_NEEDED,
    "discovery.operator_question": OPERATOR_ACTION_NEEDED,
    # High-signal → operator-fyi
    "artifact_handoff_failed": OPERATOR_ACTION_NEEDED,
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
    "strategic_steering_deferred": OPERATOR_FYI,
    "strategic_steering_acted": OPERATOR_FYI,
    # Trial progress/verdicts are platform control-loop bookkeeping, not
    # project-strategy context. Keep them out of both the operator transcript
    # and the strategist payload; only an ambiguous-lift escalation belongs in
    # the project conversation.
    "capability_trial_stalled": PIPELINE_LOG,
    "capability_verdict_decided": PIPELINE_LOG,
    "capability_trial_ambiguous_lift_escalation": OPERATOR_FYI,
    "question_answer": OPERATOR_FYI,
    # feedback_reply: user replied to a feedback/notification — informational,
    # operator-visible. The strategist is armed (via maybe_arm_operator_refresh)
    # to process the answer, same as question_answer.
    "feedback_reply": OPERATOR_FYI,
    # Capability-proof gate auto-graduation — an autonomous gate-mode change.
    # off→observe is informational (nothing blocks yet); →enforce begins
    # BLOCKING task spawning and release promotion, so it needs attention.
    "capability_proof_gate_graduated_observe": OPERATOR_FYI,
    "capability_proof_gate_graduated_enforce": OPERATOR_ACTION_NEEDED,
    # Shared mode-knob graduation harness (app.mode_knob_graduation). Same split
    # as the capability-proof gate: off→observe is informational; →enforce turns
    # on a real blocking branch, so it needs attention. A measured CUT
    # recommendation (the enforce branch would never bind) is FYI — advisory
    # signal the operator acts on by removing the knob.
    "mode_knob_graduated_observe": OPERATOR_FYI,
    "mode_knob_graduated_enforce": OPERATOR_ACTION_NEEDED,
    "mode_knob_cut_recommended": OPERATOR_FYI,
    # Criterion mutation removes/replaces a quality gate → operator-action-needed
    "criterion_retracted": OPERATOR_ACTION_NEEDED,
    "criterion_superseded": OPERATOR_ACTION_NEEDED,
    # alert_dispatch._maybe_create_problem_model_wipe_risk_alert: an agent write
    # proposed dropping ALL entries of a problem-model field without an explicit
    # _deletions directive (merged non-destructively). The operator should
    # investigate recent problem_model writes → operator-action-needed.
    "problem_model.wipe_risk": OPERATOR_ACTION_NEEDED,
    # Directly-authored operator escalations (previously unregistered → landed
    # with a NULL audience that both read filters fail OPEN on).  Registering
    # them with their intended, operator-facing audience makes the audience gate
    # structurally fail-closed at the emitter: the message is stamped by
    # ``stamp_message_audience`` at construction instead of relying on the
    # read-side NULL fall-through.
    #
    # review_loop_convergence._create_handmerge_attention_message: a preserved
    # hand-merge PR needs the operator to merge it by hand.
    "handmerge_pr_preserved": OPERATOR_ACTION_NEEDED,
    # branch_resolution.signal_unsafe_branch_configuration: a blocking-class
    # branch-safety BLOCKER — fleet work would route to the promotion target.
    "unsafe_branch_configuration": OPERATOR_ACTION_NEEDED,
    # review_fix_lane_pilot.raise_gate_integrity_alert: a non-negotiable
    # gate-integrity violation BLOCKER.
    "review_fix_lane_gate_integrity_violation": OPERATOR_ACTION_NEEDED,
    # review_pipeline._post_repeat_fingerprint_blocker_message: a repeat-fingerprint
    # halt BLOCKER — the operator must decide whether to spawn a precondition-fix
    # direction or cancel the chain.
    "repeat_fingerprint_halt": OPERATOR_ACTION_NEEDED,
    # review_fix_lane_pilot.emit_lane_event_message: per-lane terminal/degraded
    # WARNING notices the operator currently sees.  Kept operator-fyi to preserve
    # their current visibility while closing the structural NULL hole; a later
    # messaging-redesign task may reclassify lane bookkeeping to strategist-internal.
    "lane_killed_exhausted": OPERATOR_FYI,
    "lane_killed_stranded": OPERATOR_FYI,
    "lane_checkpoint_recover": OPERATOR_FYI,
    "lane_budget_near_exhausted": OPERATOR_FYI,
    "pilot_auto_disabled": OPERATOR_FYI,
    # review_park_disposition._post_disposition_note: the operator-disposition
    # window expired and the sweep abandoned the park on its own.  This is a
    # record of a decision already taken, not a request for one -- the whole
    # point of the sweep is that nobody has to answer it -- and the PR is left
    # OPEN, so nothing irreversible happened.  Operator-fyi, never
    # operator-action-needed.
    "review_park_disposed": OPERATOR_FYI,
    # === Alert event types (from alert_policy.ALERT_EVENT_POLICIES) =======
    # Core portable alerts — operator-facing
    "task.failed": OPERATOR_FYI,
    "task.blocked": OPERATOR_FYI,
    "approval.needed": OPERATOR_ACTION_NEEDED,
    # A PR-less approval-gate timeout parked work instead of failing it; the
    # operator must approve, deny or requeue before the hold self-releases.
    "approval.timeout_held": OPERATOR_ACTION_NEEDED,
    "approval.merge_checked_base_mismatch": OPERATOR_ACTION_NEEDED,
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
    # runner.update_drain_stuck: same update-failure family as
    # runner.post_update_silent — the runner is stuck idle in auto-update
    # drain (accepting no claims) instead of going silent outright. Same
    # operator remediation (check the runner journal / redeploy), so it
    # gets the same audience.
    "runner.update_drain_stuck": OPERATOR_ACTION_NEEDED,
    "runner.cross_project_access": OPERATOR_ACTION_NEEDED,
    # Execution placement: a local_only project with no eligible runner.
    "placement.unsatisfiable_local_hold": OPERATOR_ACTION_NEEDED,
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
    "release_blocked": OPERATOR_ACTION_NEEDED,
    "release.calcifying": OPERATOR_FYI,
    "release.ci_unresolved": OPERATOR_FYI,
    "release.build_stalled": OPERATOR_FYI,
    "release.stabilize_stalled": OPERATOR_FYI,
    "release.approved_pr_stranded": OPERATOR_FYI,
    "release.unshipped_checkpoint_stranded": OPERATOR_ACTION_NEEDED,
    "release.promotion_deploy_failed": OPERATOR_ACTION_NEEDED,
    "release.managed_check_wait_stalled": OPERATOR_FYI,
    "release.qualification_timeout": OPERATOR_ACTION_NEEDED,
    "release.qualification_crash_loop": OPERATOR_ACTION_NEEDED,
    "release.qualification_trigger_unowned": OPERATOR_ACTION_NEEDED,
    "release.qualification_ci_compliance_block": OPERATOR_FYI,
    "release.walkthrough_key_unmatched": OPERATOR_ACTION_NEEDED,
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
    "research.direction_auto_retired": STRATEGIST_INTERNAL,
    "research.churn_retirement_recommended": STRATEGIST_INTERNAL,
    "research.anomaly_auto_adjusted": STRATEGIST_INTERNAL,
    "research.escalation_delivery_failed": OPERATOR_ACTION_NEEDED,
    "research.diagnosis_stalled": OPERATOR_ACTION_NEEDED,
    "research.strategic_cycle_liveness": OPERATOR_ACTION_NEEDED,
    "research.strategic_issue": OPERATOR_FYI,
    # Dedup / already-fixed deadlock diagnostics: internal pipeline health
    # signals handled by the strategic agent — not surfaced to the operator.
    "research.dedup_deadlock": STRATEGIST_INTERNAL,
    "research.already_fixed_deadlock": STRATEGIST_INTERNAL,
    # Zero-children NOTE: strategic bookkeeping when a research pass creates no
    # child tasks (all findings deduped or no actionable scope).
    "research.zero_children": STRATEGIST_INTERNAL,
    # Branch lifecycle bookkeeping
    "branch.cleanup_summary": STRATEGIST_INTERNAL,
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
    "cost.telemetry_reconciliation_drift": OPERATOR_FYI,
    "efficiency.self_repair_burn_share": STRATEGIST_INTERNAL,
    "efficiency.provider_routing_verdict": STRATEGIST_INTERNAL,
    # === Compute infrastructure alerts ====================================
    # compute.image_drift: baked compute image was auto-rebuilt due to
    # dependency manifest change — informational, operator-visible.
    "compute.image_drift": OPERATOR_FYI,
    # compute.image_drift_deferred: dependency manifest changed but the
    # rebuild was deferred (builder busy or build budget exhausted) —
    # informational, operator-visible.
    "compute.image_drift_deferred": OPERATOR_FYI,
    # compute.image_protect_fail: GC-protection tag sweep failed — the image
    # digest may be reclaimed by registry retention before being replaced.
    # Operator should verify gcloud / Artifact Registry availability.
    "compute.image_protect_fail": OPERATOR_FYI,
    # compute.image_build_failure: baked compute image build failed — authoring
    # agents and compute jobs may use a stale or missing image until retried.
    # Operator-visible so the build failure appears in the alert surface.
    "compute.image_build_failure": OPERATOR_FYI,
    # compute.image_refresh_failure: live Cloud Build refresh lane returned a
    # failure status for a project's compute image — stale images until retried.
    "compute.image_refresh_failure": OPERATOR_FYI,
    # === Research / direction drift =======================================
    # research.direction_drift: direction claim vs. shipped-code drift
    # detected — operator should review the flagged directions.
    "research.direction_drift": OPERATOR_FYI,
    # === Back-merge / hotfix alerts (no existing policy entry) ============
    "hotfix_backmerge.size_violation": STRATEGIST_INTERNAL,
    # === Service/background-process lifecycle alerts ========================
    # Background service crashed (one-off) or is flapping (repeated crashes).
    "service.crashed": OPERATOR_ACTION_NEEDED,
    "service.crash_episode": OPERATOR_ACTION_NEEDED,
    # Auto-restart of a background service is informational internal bookkeeping.
    "service.restarted": STRATEGIST_INTERNAL,
    # === Task-level alerts =================================================
    # A prerequisite task failed and cascade-cancelled dependent tasks.
    "task.cascade_cancelled": OPERATOR_FYI,
    # Post-commit orphan task auto-recovered by spawning a review pipeline.
    "task.orphan_recovered": STRATEGIST_INTERNAL,
    # === Deployment-level alerts ===========================================
    # Orchestrator process hit consecutive exceptions — pipeline may be stalled.
    "deployment.orchestrator_error_loop": OPERATOR_ACTION_NEEDED,
    # Wake-delivery sweep is stalled; PARKED tasks with fired wake sources not advancing.
    "deployment.wake_delivery_stalled": OPERATOR_ACTION_NEEDED,
    # Baked compute image auto-rebuilt due to manifest drift (pipeline internal).
    "deployment.compute_image_rebuilt": STRATEGIST_INTERNAL,
    # Hotfix promotion bypassed the standard qualification gate.
    "release.hotfix_bypass": OPERATOR_FYI,
    # === Compute job alerts =================================================
    # Compute job terminated by the no-progress watchdog.
    "compute.job_stalled": OPERATOR_FYI,
    # === Runner-specific alerts =============================================
    # Runner self-update bookkeeping: INFO (succeeded) and WARNING (failed)
    # variants both record internal pipeline housekeeping — the operator
    # does not need to act on a successful update, and a failed update
    # surfaces via runner.degraded / post_update_silent, which ARE operator-
    # visible.  Registering here ensures operator_visible_alert_filter drops
    # these rows when they leak through direct Alert() construction.
    "runner.self_update": STRATEGIST_INTERNAL,
    # Task with require_local_runner fell back to cloud after threshold.
    "runner.local_fallback": OPERATOR_FYI,
    # Runner repeatedly declined a task (repeated no-bid signals).
    "runner.task_declined": OPERATOR_FYI,
    # No self-hosted runner online for a task that requires one (OPERATOR_ACTION_NEEDED
    # because operator must provision/restart the required runner fleet).
    "runner.local_runner_unavailable": OPERATOR_ACTION_NEEDED,
    # No Docker-capable runner online for a task that needs a container image.
    "runner.container_runner_unavailable": OPERATOR_ACTION_NEEDED,
    # Task pinned to local runner skipped because LLM credentials cannot be resolved.
    "runner.credential_skip": OPERATOR_ACTION_NEEDED,
    # === Billing / budget alerts ============================================
    # Monthly budget threshold alert — operator should review spend and act.
    "MONTHLY_BUDGET_ALERT": OPERATOR_ACTION_NEEDED,
    # Daily spend budget reached — autonomous spawning is paused for the day.
    "DAILY_BUDGET_ALERT": OPERATOR_ACTION_NEEDED,
    # === SR quality metrics (strategist-internal, acted on autonomously) ====
    "sr.precision_recall_degraded": STRATEGIST_INTERNAL,
    "sr.engagement_thinning": STRATEGIST_INTERNAL,
    "sr.false_attestation": STRATEGIST_INTERNAL,
    # === Account management alerts ==========================================
    # Pool account force-deleted while active tasks still held it.
    "account.force_deleted_with_active_tasks": OPERATOR_ACTION_NEEDED,
    # === Capacity management (internal pipeline) ============================
    # Task deferred because project hit max concurrent compute jobs cap.
    "capacity.compute_cap_deferred": STRATEGIST_INTERNAL,
    # Usage meter persistently dark — LLM account(s) unreachable or persist-
    # failing for >30 min, which can prevent usage_headroom from arming.
    # Operator-visible so a chronically broken meter does not go unnoticed.
    "capacity.usage_meter_dark": OPERATOR_FYI,
    # === Infrastructure alerts =============================================
    # Compute image GC-protection tag failed to apply; risk of digest loss.
    "vm.gc_protection_failure": OPERATOR_ACTION_NEEDED,
    # work_session_command_execution: operator command receipts (confirmed/failed).
    # These are resolved NOTEs that record an operator-initiated command result —
    # visible in the conversation panel but not an escalation requiring attention.
    "operator_command": OPERATOR_FYI,
    # admin_project_strategic_session: operator check-in notes. A check-in is a
    # short user-initiated note to the strategic agent; it is already resolved on
    # creation and visible in the conversation surface.
    "strategic_check_in": OPERATOR_FYI,
    # admin_agent_ops.agent_annotate: agent-authored annotations (comment, issue,
    # vision). Annotations are agent outputs that belong in the strategic context;
    # they are operator-visible so the operator can see the agent's findings without
    # needing a separate escalation channel.
    "agent_annotation": OPERATOR_FYI,
    # admin_agent_ops.agent_annotate: size_breach annotations are programmatic
    # signals from a test-invariant failure — the same class of internal pipeline
    # bookkeeping as self_improvement.payload_too_large. They must NOT reach the
    # operator channel; the strategist acts on them autonomously.
    "agent_annotation.size_breach": STRATEGIST_INTERNAL,
    # admin_agent_ops.strategic_operation / strategic_completion: catch-all for
    # strategic messages whose event_type is unknown or missing. Unregistered types
    # already fail-closed to STRATEGIST_INTERNAL via route_event; this entry makes
    # the fallback explicit so it is self-documenting and test-stable.
    "strategic_message": STRATEGIST_INTERNAL,
    # Strategist self-observability. A chronically-low operator-decision
    # capture rate means operator corrections are silently failing to reach the
    # durable decisions register. This is internal strategist bookkeeping — the
    # strategist acts on it (re-emit the marker) but it must NEVER reach the
    # operator chat or the alert channel.
    "operator_decision.capture_rate_low": STRATEGIST_INTERNAL,
    # operator_decision.summary_absent: the resident agent's heartbeats stopped
    # carrying operator_decision_summary for longer than the staleness window
    # despite the session having enough historical data to judge it. Strategist-
    # internal so the strategist can investigate without creating operator noise.
    "operator_decision.summary_absent": STRATEGIST_INTERNAL,
    # operator_inbox_leak_persistent: durable persistent-leak escalation from the
    # operator-inbox liveness audit.  Strategist-internal so a persistent
    # forward-leak / alert-noise signal reaches the strategist surface without
    # creating new operator-facing noise (the very bug this signal detects).
    "operator_inbox_leak_persistent": STRATEGIST_INTERNAL,
    # delivery.reply_quarantine_unhealed: an operator-directed reply was stamped
    # with a non-operator audience (quarantined / never delivered to the operator).
    # Fires once per episode when the liveness checker detects quarantined replies;
    # OPERATOR_ACTION_NEEDED so the operator sees that their reply was dropped and
    # a heal was attempted.  Auto-resolves when the quarantine count reaches zero.
    "delivery.reply_quarantine_unhealed": OPERATOR_ACTION_NEEDED,
    # delivery.undelivered_strategist_reply: an operator-directed strategist reply
    # has an operator-visible audience but was never delivered (delivered_at IS NULL)
    # past the staleness threshold (~15 min).  Fires once per episode when the
    # liveness checker detects such replies; OPERATOR_ACTION_NEEDED so the operator
    # sees that their answer was lost.  Auto-resolves on a clean tick (zero
    # undelivered replies).  Distinguished from delivery.reply_quarantine_unhealed
    # because the audience IS operator-visible — the message was routed correctly
    # but the delivery receipt itself is missing, indicating a transport-level loss.
    "delivery.undelivered_strategist_reply": OPERATOR_ACTION_NEEDED,
    # notification.dead_letter_lost: a notification exhausted all retries AND the
    # subsequent dead-letter persistence write also failed — the notification is
    # now unrecoverable.  OPERATOR_ACTION_NEEDED so the operator sees that a
    # webhook notification was permanently lost (double-failure path).
    "notification.dead_letter_lost": OPERATOR_ACTION_NEEDED,
    # problem_model.live_core_over_budget: the problem model's live core (active
    # gaps, capabilities, friction) exceeds the byte budget at write time —
    # the precondition for the rev-1367 truncation/wipe cycle.  OPERATOR_ACTION_NEEDED
    # so the operator sees the alert and can prune the model or raise the byte budget.
    "problem_model.live_core_over_budget": OPERATOR_ACTION_NEEDED,
}

# Set of audiences that reach the operator through normal channels.
_AUDIENCES_THAT_REACH_OPERATOR: frozenset[str] = frozenset(
    {OPERATOR_ACTION_NEEDED, OPERATOR_FYI}
)

_NON_OPERATOR_AUDIENCES: frozenset[str] = frozenset(
    {STRATEGIST_INTERNAL, PIPELINE_LOG}
)

# Registry event_types that resolve to a non-operator audience.  Precomputed
# from the single-source-of-truth registry.  Used by get_non_operator_event_types()
# (a testing hook); the Alert read-model filter now uses _OPERATOR_EVENT_TYPES
# (explicit allowlist, fail-closed) rather than this denylist.
_NON_OPERATOR_EVENT_TYPES: frozenset[str] = frozenset(
    event_type
    for event_type, audience in _EVENT_AUDIENCE_REGISTRY.items()
    if audience in _NON_OPERATOR_AUDIENCES
)

# Registry event_types that resolve to an operator-visible audience.  Precomputed
# for use by operator_visible_alert_filter: the Alert read-model uses an explicit
# IN-allowlist (fail-closed) so NULL and unregistered event_types never reach an
# operator badge, count, or preview.
_OPERATOR_EVENT_TYPES: frozenset[str] = frozenset(
    event_type
    for event_type, audience in _EVENT_AUDIENCE_REGISTRY.items()
    if audience in _AUDIENCES_THAT_REACH_OPERATOR
)


# Message types that are inherently operator-facing when no event_type
# overrides the audience.  A strategist-authored BLOCKER/BUG_REPORT/QUESTION
# with no registered (or absent) event_type must reach the operator inbox even
# though the fail-closed default resolves STRATEGIST_INTERNAL.
_OPERATOR_FACING_MESSAGE_TYPES: frozenset[str] = frozenset(
    {"BUG_REPORT", "BLOCKER", "QUESTION"}
)


# ── Public API ──────────────────────────────────────────────────────────


def route_event(event_type: str, *, message_type: str | None = None) -> str:
    """Resolve the audience for *event_type*.

    Fail-closed: returns ``STRATEGIST_INTERNAL`` for any event_type that
    is not registered — a new emitter that forgets to declare its audience
    is invisible to the operator.

    ``message_type`` fallback: when the event_type resolves to
    ``STRATEGIST_INTERNAL`` (absent, unregistered, or explicitly internal)
    *and* ``message_type`` is in ``{BUG_REPORT, BLOCKER, QUESTION}``, the
    audience is upgraded to ``OPERATOR_ACTION_NEEDED`` — these message classes
    are inherently operator-facing regardless of the event_type.  An
    event_type that explicitly resolves to an operator-visible audience always
    wins; the fallback only fires when the registry alone would bury the message.
    """
    normalized = (event_type or "").strip()
    resolved = _EVENT_AUDIENCE_REGISTRY.get(normalized, STRATEGIST_INTERNAL) if normalized else STRATEGIST_INTERNAL
    if resolved == STRATEGIST_INTERNAL and message_type and message_type.upper() in _OPERATOR_FACING_MESSAGE_TYPES:
        return OPERATOR_ACTION_NEEDED
    return resolved


def is_operator_visible(audience: str) -> bool:
    """Return True when *audience* is operator-action-needed or operator-fyi."""
    return audience in _AUDIENCES_THAT_REACH_OPERATOR


def operator_visible_audience_filter(context: Any) -> Any:
    """Return the shared SQL filter for Task rows and other legacy/NULL-retaining paths.

    Task rows carry a NULL audience by design (tasks are never audience-stamped),
    so this filter retains NULL rows alongside explicitly operator-visible rows.
    Use ONLY for ``Task.context`` call sites — for ``ProjectMessage.context`` use
    :func:`operator_visible_message_audience_filter` (fail-closed, no NULL pass-through).
    """
    audience = context["audience"].as_string()
    return or_(audience.is_(None), audience.notin_(_NON_OPERATOR_AUDIENCES))


def operator_visible_message_audience_filter(context: Any) -> Any:
    """Return the fail-closed SQL filter for all ``ProjectMessage.context`` query paths.

    Uses an explicit IN-allowlist of operator audiences — no NULL pass-through.
    NULL-audience messages (legacy un-stamped rows) are treated as non-operator,
    consistent with the emitter-side fail-closed default via :func:`route_event`.

    All open NULL-audience messages are drained by the proactive backfill sweep
    (``reaper_sweeps.stamp_all_null_audience_messages``), and every message emitter
    stamps a canonical audience at construction, so no legitimate operator message
    is NULL after the drain.

    Use this for every ``ProjectMessage.context`` query path.  For ``Task.context``
    (tasks are never audience-stamped), use :func:`operator_visible_audience_filter`.
    """
    audience = context["audience"].as_string()
    return audience.in_(list(_AUDIENCES_THAT_REACH_OPERATOR))


def operator_visible_nonactionable_message_filter(context: Any) -> Any:
    """Return the strict SQL filter for non-actionable (NOTE/FEEDBACK) messages.

    Identical semantics to :func:`operator_visible_message_audience_filter` —
    fail-closed IN-allowlist, no NULL pass-through.  Kept as a named alias for
    query paths that are already restricted to non-BLOCKER/QUESTION/BUG_REPORT
    message types (e.g. high-signal NOTE counts in operator_attention.py).
    """
    audience = context["audience"].as_string()
    return audience.in_(list(_AUDIENCES_THAT_REACH_OPERATOR))


def operator_visible_alert_filter(event_type_column: Any) -> Any:
    """Return the shared SQL filter for operator-visible ``Alert`` rows.

    ``Alert`` carries no ``context``/``audience`` column — its audience is keyed
    on ``event_type`` through the same registry that :func:`route_event`
    consults at creation.  This filter is fail-closed: only rows whose
    ``event_type`` is explicitly registered as operator-visible
    (``operator-action-needed`` or ``operator-fyi``) pass through.
    NULL and unregistered ``event_type`` rows are excluded — they must never
    contribute to an operator-facing badge count, severity aggregate, or preview.
    Register the event_type in :data:`_EVENT_AUDIENCE_REGISTRY` with the correct
    operator audience to make an Alert visible.
    """
    return event_type_column.in_(list(_OPERATOR_EVENT_TYPES))


def get_non_operator_event_types() -> frozenset[str]:
    """Return every event_type that resolves to a non-operator audience (testing hook)."""
    return _NON_OPERATOR_EVENT_TYPES


def get_operator_event_types() -> frozenset[str]:
    """Return every event_type that resolves to an operator-visible audience (testing hook)."""
    return _OPERATOR_EVENT_TYPES


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
    "STRATEGIC_ENGINE_HALTED": OPERATOR_ACTION_NEEDED,
    # --- Runner-health fleet incidents — operator-actionable outages. ---
    "RUNNER_DEACTIVATED": OPERATOR_ACTION_NEEDED,
    "RUNNER_DEGRADED": OPERATOR_FYI,
    "RUNNER_DEGRADED_ESCALATED": OPERATOR_ACTION_NEEDED,
    "RUNNER_FLEET_OFFLINE": OPERATOR_ACTION_NEEDED,
    "RUNNER_LOW_DISK": OPERATOR_ACTION_NEEDED,
    "RUNNER_LOW_MEMORY": OPERATOR_ACTION_NEEDED,
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


# ── Logical event pair registry ──────────────────────────────────────────
#
# Many events appear in BOTH the internal-ledger registry (lowercase/dotted)
# and the external-webhook registry (UPPERCASE_UNDERSCORE) under different
# naming conventions, but represent the same logical operation.  Paired entries
# MUST agree on audience classification.  This mapping is the single source of
# truth for those equivalences; ``test_message_audience.py`` reads it and fails
# CI when a pair drifts so silent inconsistencies are caught before shipping.
#
# Only register pairs that are genuinely the same logical event.  Events that
# only exist in one registry (internal-only bookkeeping or webhook-only fields)
# must NOT appear here.
_LOGICAL_EVENT_PAIRS: dict[str, str] = {
    # internal_key (lowercase/dotted): webhook_key (UPPERCASE_UNDERSCORE)
    "pr_merged": "PR_MERGED",
    "auto_fix_escalated": "AUTO_FIX_ESCALATED",
    "auto_fix_spawned": "AUTO_FIX_SPAWNED",
    "residual_fix_escalated": "RESIDUAL_FIX_ESCALATED",
    "residual_fix_audit_deferred": "RESIDUAL_FIX_AUDIT_DEFERRED",
    "residual_fix_spawned": "RESIDUAL_FIX_SPAWNED",
    "residual_fix_unsuspended": "RESIDUAL_FIX_UNSUSPENDED",
    "direction_burn_pause": "DIRECTION_BURN_PAUSE",
    "remediation_escalated": "REMEDIATION_ESCALATED",
    "merge.unverified": "MERGE_UNVERIFIED",
    "task.failed": "TASK_FAILED",
    "approval.needed": "APPROVAL_NEEDED",
    "runner.deactivated": "RUNNER_DEACTIVATED",
    "runner.degraded": "RUNNER_DEGRADED",
    "runner.low_disk": "RUNNER_LOW_DISK",
    "runner.low_memory": "RUNNER_LOW_MEMORY",
    "research.strategic_cycle_liveness": "STRATEGIC_ENGINE_HALTED",
}


def get_logical_event_pairs() -> dict[str, str]:
    """Return the known internal→webhook event equivalences (testing hook)."""
    return dict(_LOGICAL_EVENT_PAIRS)


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
