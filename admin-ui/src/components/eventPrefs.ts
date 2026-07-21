import { type TemporaryMutes, isEventTypeTemporarilyMuted } from './temporaryMutes';

// Canonical set of escalation event types emitted by backend/app/notification_dispatch.py.
// One source of truth: high-signal classification, error toast severity, and ContextBadges
// all read from this set, so a new escalation type added here automatically gets surfaced
// across all three behaviors. Keep in sync with notification_dispatch.py escalation emitters.
//
// EVENT TYPE INVARIANT: See backend/testing/event_type_snapshot.json for the canonical list
// of all 21+ event_types emitted by the backend. The invariant test in
// MessagesPanel.invariant.test.ts validates that every event_type appears in either
// HIGH_SIGNAL_NOTE_EVENTS or ROUTINE_EVENT_LABELS below. When adding a new event_type:
// 1. Update backend/testing/event_type_snapshot.json with (name, classification, source_fn)
// 2. Update HIGH_SIGNAL_NOTE_EVENTS, ROUTINE_EVENT_LABELS, or ESCALATION_EVENT_TYPES below
// 3. If escalation, add a badge label to ESCALATION_BADGE_LABELS
// The invariant test will fail and tell you exactly what's missing.
export const ESCALATION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'auto_fix_escalated',
  'residual_fix_escalated',
  'residual_fix_audit_deferred',
  'remediation_escalated',
  'direction_burn_pause',
  'direction_churn_pause',
  'direction_outcome_scaffolding_escalation',
]);

// Per-event-type label and Tailwind class for the ContextBadges escalation badge.
// Adding a new entry to ESCALATION_EVENT_TYPES without a corresponding entry here
// is caught by the invariant test in MessagesPanel.utils.test.ts.
export const ESCALATION_BADGE_LABELS: Readonly<Record<string, string>> = {
  auto_fix_escalated: 'Auto-fix Escalated',
  residual_fix_escalated: 'Residual Fix Escalated',
  residual_fix_audit_deferred: 'Audit Deferred',
  remediation_escalated: 'Remediation Escalated',
  direction_burn_pause: 'Burn Pause',
  direction_churn_pause: 'Churn Pause',
  direction_outcome_scaffolding_escalation: 'Scaffolding Escalation',
};

// Badge labels for high-signal NOTE event types that are NOT escalations.
// Every entry in HIGH_SIGNAL_NOTE_EVENTS that is not in ESCALATION_EVENT_TYPES
// should have an entry here so ContextBadges can render a red/amber badge
// without a separate else-if branch per type. The invariant test in
// MessagesPanel.utils.test.ts guards against drift.
export const HIGH_SIGNAL_NOTE_BADGE_LABELS: Readonly<Record<string, string>> = {
  failure_alert: 'Failure Alert',
  rollback_recommended: 'Rollback Recommended',
  fix_loop_divergence_regressed: 'Fix Loop Diverged',
  release_calcifying: 'Release Calcifying',
  direction_outcome_stalled: 'Direction Stalled',
  release_build_stalled: 'Build Stalled',
  release_stabilize_stalled: 'Stabilize Stalled',
  release_ci_unresolved_stall: 'CI Stall',
  release_auto_merge_stalled: 'Auto-merge Stalled',
  promotion_readiness_reached: 'Promotion Ready',
  strategic_steering_deferred: 'Steering Queued',
  strategic_steering_acted: 'Steering Reached',
  direction_staged_activated: 'Direction Activated',
  capability_trial_stalled: 'Trial Stalled',
  capability_verdict_decided: 'Verdict Decided',
  capability_trial_ambiguous_lift_escalation: 'Ambiguous Lift',
  question_answer: 'Question Answered',
  criterion_retracted: 'Criterion Retracted',
  criterion_superseded: 'Criterion Superseded',
  capability_proof_gate_graduated_observe: 'Proof Gate → Observe',
  capability_proof_gate_graduated_enforce: 'Proof Gate → Enforce',
};

export const HIGH_SIGNAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'release_calcifying', 'direction_outcome_stalled', 'release_build_stalled',
  'release_stabilize_stalled', 'release_ci_unresolved_stall', 'release_auto_merge_stalled',
  'promotion_readiness_reached', 'strategic_steering_deferred', 'strategic_steering_acted',
  'direction_staged_activated', 'capability_trial_stalled', 'capability_verdict_decided',
  'capability_trial_ambiguous_lift_escalation',
  'question_answer', 'criterion_retracted', 'criterion_superseded',
  'capability_proof_gate_graduated_observe', 'capability_proof_gate_graduated_enforce',
  ...ESCALATION_EVENT_TYPES,
]);

export const CRITICAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'release_calcifying', 'direction_outcome_stalled', 'release_build_stalled',
  'release_stabilize_stalled', 'release_ci_unresolved_stall', 'release_auto_merge_stalled',
  'promotion_readiness_reached', 'capability_trial_stalled',
  ...ESCALATION_EVENT_TYPES,
]);

export const ROUTINE_EVENT_LABELS: Record<string, string> = {
  pr_merged: 'PR merges',
  direction_milestone: 'milestones',
  cycle_summary: 'cycle summaries',
  account_model_regression_quarantined: 'account quarantines',
  account_model_regression_recovered: 'account recoveries',
  auto_fix_spawned: 'auto-fix spawns',
  auto_fix_result: 'auto-fix results',
  residual_fix_spawned: 'residual fix spawns',
  residual_fix_result: 'residual fix results',
  residual_fix_unsuspended: 'residual unsuspensions',
  residual_case_auto_resolved: 'auto-resolutions',
  issue_implementation_spawned: 'issue spawns',
  review_feedback: 'review feedback',
  feedback_acknowledgment: 'feedback acks',
  'operator_decision.capture_rate_low': 'op-decision capture rate',
};

// Event types that the server stamps as audience=strategist-internal. All entries
// in ROUTINE_EVENT_LABELS are strategist-internal per event_type_snapshot.json,
// so this set is derived from those keys. Used as a fallback block in
// classifySignalLevel: if a message arrives without an audience stamp (legacy),
// we must not let HIGH_SIGNAL_NOTE_EVENTS drift accidentally re-promote an event
// that the server designates strategist-internal.
// Invariant: STRATEGIST_INTERNAL_FALLBACK_BLOCK ∩ HIGH_SIGNAL_NOTE_EVENTS = ∅
// Pinned by: MessagesPanel.invariant.test.ts
export const STRATEGIST_INTERNAL_FALLBACK_BLOCK: ReadonlySet<string> = new Set(
  Object.keys(ROUTINE_EVENT_LABELS),
);

export type EventPref = 'mute' | 'routine' | 'surfaced';

export const EVENT_PREFS_KEY = 'messages_panel_event_prefs';

// Legacy global key for migration only
const LEGACY_EVENT_PREFS_KEY = 'messages_panel_event_prefs';

export function eventPrefsKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_event_prefs:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadEventPrefs(userId?: string | null, projectId?: string | null): Record<string, EventPref> {
  try {
    const key = eventPrefsKey(userId || undefined, projectId || undefined);
    const stored = localStorage.getItem(key);
    if (stored) return JSON.parse(stored);

    // Migration: if no project-scoped key exists, try legacy global key
    const legacy = localStorage.getItem(LEGACY_EVENT_PREFS_KEY);
    if (legacy) return JSON.parse(legacy);

    return {};
  } catch {
    return {};
  }
}

export function saveEventPrefs(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  prefs: Record<string, EventPref>,
): void {
  try {
    const key = eventPrefsKey(userId || undefined, projectId || undefined);
    if (Object.keys(prefs).length === 0) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(prefs));
    }
  } catch { /* ignore */ }
}

export function classifySignalLevel(msg: {
  message_type: string;
  from_type: string;
  context: Record<string, unknown>;
}): 'high' | 'routine' {
  const audience = msg.context.audience as string | undefined;
  if (audience) {
    if (audience === 'operator-action-needed') return 'high';
    if (audience === 'strategist-internal' || audience === 'pipeline-log') return 'routine';
    if (audience === 'operator-fyi') return 'routine';
  }
  // Legacy fallback for messages without a server-stamped audience
  if ((msg.from_type || '').toLowerCase() === 'user') return 'high';
  if (['FEEDBACK', 'BLOCKER', 'BUG_REPORT', 'QUESTION'].includes(msg.message_type)) return 'high';
  if (msg.message_type === 'NOTE') {
    const et = msg.context.event_type as string | undefined;
    // Guard: strategist-internal events must never be re-promoted by the
    // HIGH_SIGNAL_NOTE_EVENTS fallback, even if they drift into that set.
    if (et && STRATEGIST_INTERNAL_FALLBACK_BLOCK.has(et)) return 'routine';
    if (et && HIGH_SIGNAL_NOTE_EVENTS.has(et)) return 'high';
  }
  return 'routine';
}

export function effectiveSignal(
  msg: { message_type: string; from_type: string; context: Record<string, unknown> },
  prefs: Record<string, EventPref>,
  tempMutes?: TemporaryMutes,
): 'high' | 'routine' | 'mute' {
  // Defense in depth: non-operator audiences can never be promoted by local prefs.
  // The backend already filters these from operator surfaces, but the client must
  // not re-promote them via event_type heuristics or a 'surfaced' pref.
  const audience = msg.context.audience as string | undefined;
  if (audience === 'strategist-internal' || audience === 'pipeline-log') return 'routine';

  const et = msg.context.event_type as string | undefined;
  if (tempMutes && isEventTypeTemporarilyMuted(et, tempMutes)) return 'mute';
  const pref = et ? prefs[et] : undefined;
  if (pref === 'mute') return 'mute';
  if (pref === 'routine') return 'routine';
  if (pref === 'surfaced') return 'high';
  return classifySignalLevel(msg);
}

export function getToastSeverity(msg: { message_type: string; from_type: string; context: Record<string, unknown> }): 'error' | 'info' {
  const audience = msg.context.audience as string | undefined;
  if (audience === 'operator-action-needed') return 'error';
  // Non-operator audiences never warrant an error toast
  if (audience === 'strategist-internal' || audience === 'pipeline-log') return 'info';
  // operator-fyi is an operator-reaching but informational stamp — the server has
  // already decided it is not action-needed, so it never warrants an error toast
  // even when the legacy message_type would (matches classifySignalLevel='routine').
  if (audience === 'operator-fyi') return 'info';
  const et = msg.context.event_type as string | undefined;
  if (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT' || msg.message_type === 'QUESTION') return 'error';
  if (et && CRITICAL_NOTE_EVENTS.has(et)) return 'error';
  return 'info';
}

export function isCriticalSeverity(msg: {
  message_type: string;
  context: Record<string, unknown>;
}): boolean {
  const audience = msg.context.audience as string | undefined;
  if (audience === 'operator-action-needed') return true;
  // Non-operator audiences are never critical from the operator's perspective
  if (audience === 'strategist-internal' || audience === 'pipeline-log') return false;
  // operator-fyi reaches the operator but is informational, not action-needed —
  // the server-stamped audience is authoritative, so it is never critical even
  // when the legacy message_type would be (matches classifySignalLevel='routine').
  if (audience === 'operator-fyi') return false;
  if (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT' || msg.message_type === 'QUESTION') return true;
  if (msg.message_type === 'NOTE') {
    const et = msg.context.event_type as string | undefined;
    if (et && CRITICAL_NOTE_EVENTS.has(et)) return true;
  }
  return false;
}

export function countHighSignalMessages(
  messages: Array<{ message_type: string; from_type: string; status: string; context: Record<string, unknown> }>,
  prefs?: Record<string, EventPref>,
  tempMutes?: TemporaryMutes,
): number {
  return messages.filter(m =>
    m.status === 'open' && (prefs ? effectiveSignal(m, prefs, tempMutes) : classifySignalLevel(m)) === 'high'
  ).length;
}

export function countNewSinceLastVisit(
  messages: Array<{ message_type: string; from_type: string; status: string; context: Record<string, unknown>; created_at?: string | null }>,
  lastSeenTs: number | null,
  prefs?: Record<string, EventPref>,
  tempMutes?: TemporaryMutes,
): number {
  if (lastSeenTs === null) return 0;
  return messages.filter(m => {
    if (m.status !== 'open') return false;
    const signal = prefs ? effectiveSignal(m, prefs, tempMutes) : classifySignalLevel(m);
    if (signal !== 'high') return false;
    if (!m.created_at) return false;
    const ts = new Date(m.created_at.endsWith('Z') || m.created_at.includes('+') ? m.created_at : m.created_at + 'Z').getTime();
    return !isNaN(ts) && ts > lastSeenTs;
  }).length;
}
