import { describe, it, expect } from 'vitest';
import {
  HIGH_SIGNAL_NOTE_EVENTS,
  ESCALATION_EVENT_TYPES,
  ROUTINE_EVENT_LABELS,
  STRATEGIST_INTERNAL_FALLBACK_BLOCK,
  classifySignalLevel,
  effectiveSignal,
  getToastSeverity,
  isCriticalSeverity,
} from './eventPrefs';
import eventTypeSnapshot from '../../../backend/testing/event_type_snapshot.json';

/**
 * Guards the frontend event classification sets against the backend audience registry.
 *
 * The backend's _EVENT_AUDIENCE_REGISTRY (message_audience.py) is the single source of
 * truth for which audiences receive each event type. The snapshot
 * (backend/testing/event_type_snapshot.json) mirrors that registry and is kept honest
 * by test_snapshot_audience_matches_runtime_registry in Python.
 *
 * This test closes the remaining gap: if a backend audience change reclassifies an event
 * to "strategist-internal", that event must not appear in HIGH_SIGNAL_NOTE_EVENTS or
 * ESCALATION_EVENT_TYPES — both of which feed the operator-facing signal surfaces.
 *
 * Non-operator audiences recognised here:
 *   "strategist-internal" — visible on the strategist surface only
 *   "pipeline-log"        — internal telemetry, not surfaced to any human inbox
 */

const NON_OPERATOR_AUDIENCES = new Set(['strategist-internal', 'pipeline-log']);

describe('eventPrefs audience registry', () => {
  it('matches_backend_audience_registry', () => {
    const violations: string[] = [];

    for (const entry of eventTypeSnapshot.event_types) {
      const { name: eventType, audience } = entry;
      if (!NON_OPERATOR_AUDIENCES.has(audience)) continue;

      if (HIGH_SIGNAL_NOTE_EVENTS.has(eventType)) {
        violations.push(
          `${eventType} (audience="${audience}") is in HIGH_SIGNAL_NOTE_EVENTS — ` +
          `non-operator-visible events must not surface in the operator inbox signal path. ` +
          `Remove it from HIGH_SIGNAL_NOTE_EVENTS in eventPrefs.ts, or update its audience ` +
          `in backend/app/message_audience.py if the reclassification was intentional.`
        );
      }

      if (ESCALATION_EVENT_TYPES.has(eventType)) {
        violations.push(
          `${eventType} (audience="${audience}") is in ESCALATION_EVENT_TYPES — ` +
          `non-operator-visible events must not trigger operator escalation toasts. ` +
          `Remove it from ESCALATION_EVENT_TYPES in eventPrefs.ts, or update its audience ` +
          `in backend/app/message_audience.py if the reclassification was intentional.`
        );
      }
    }

    expect(violations, violations.join('\n')).toHaveLength(0);
  });
});

// Helper to build a minimal message for signal classification tests
function makeTestMsg(overrides: {
  message_type?: string;
  from_type?: string;
  context?: Record<string, unknown>;
}) {
  return {
    message_type: overrides.message_type ?? 'NOTE',
    from_type: overrides.from_type ?? 'system',
    context: overrides.context ?? {},
  };
}

describe('audience-first signal classification', () => {
  describe('classifySignalLevel', () => {
    it('operator-action-needed audience → high', () => {
      expect(classifySignalLevel(makeTestMsg({ context: { audience: 'operator-action-needed' } }))).toBe('high');
    });

    it('operator-fyi audience → routine', () => {
      expect(classifySignalLevel(makeTestMsg({ context: { audience: 'operator-fyi' } }))).toBe('routine');
    });

    it('strategist-internal audience → routine even with high-signal event_type', () => {
      expect(classifySignalLevel(makeTestMsg({
        context: { audience: 'strategist-internal', event_type: 'failure_alert' },
      }))).toBe('routine');
    });

    it('pipeline-log audience → routine', () => {
      expect(classifySignalLevel(makeTestMsg({ context: { audience: 'pipeline-log' } }))).toBe('routine');
    });

    it('no audience falls back to event_type heuristic (BLOCKER → high)', () => {
      expect(classifySignalLevel(makeTestMsg({ message_type: 'BLOCKER', from_type: 'agent' }))).toBe('high');
    });

    it('no audience falls back to event_type heuristic (pr_merged → routine)', () => {
      expect(classifySignalLevel(makeTestMsg({ context: { event_type: 'pr_merged' } }))).toBe('routine');
    });
  });

  describe('effectiveSignal', () => {
    it('strategist-internal is routine even when event_type pref is surfaced', () => {
      const msg = makeTestMsg({ context: { audience: 'strategist-internal', event_type: 'failure_alert' } });
      const prefs = { failure_alert: 'surfaced' as const };
      expect(effectiveSignal(msg, prefs)).toBe('routine');
    });

    it('pipeline-log is routine even with surfaced pref', () => {
      const msg = makeTestMsg({ context: { audience: 'pipeline-log', event_type: 'cycle_summary' } });
      const prefs = { cycle_summary: 'surfaced' as const };
      expect(effectiveSignal(msg, prefs)).toBe('routine');
    });

    it('operator-action-needed with no pref override → high', () => {
      const msg = makeTestMsg({ context: { audience: 'operator-action-needed' } });
      expect(effectiveSignal(msg, {})).toBe('high');
    });

    it('operator-action-needed with mute pref is still demoted by the pref (no hard guard for operator audiences)', () => {
      const msg = makeTestMsg({ context: { audience: 'operator-action-needed', event_type: 'pr_merged' } });
      const prefs = { pr_merged: 'mute' as const };
      // The mute pref beats the audience classification for operator audiences —
      // only strategist-internal/pipeline-log have the hard guard.
      expect(effectiveSignal(msg, prefs)).toBe('mute');
    });
  });

  describe('getToastSeverity', () => {
    it('operator-action-needed → error toast', () => {
      expect(getToastSeverity(makeTestMsg({ context: { audience: 'operator-action-needed' } }))).toBe('error');
    });

    it('strategist-internal → info toast (never error)', () => {
      expect(getToastSeverity(makeTestMsg({
        context: { audience: 'strategist-internal', event_type: 'failure_alert' },
      }))).toBe('info');
    });

    it('pipeline-log → info toast', () => {
      expect(getToastSeverity(makeTestMsg({ context: { audience: 'pipeline-log' } }))).toBe('info');
    });

    it('no audience BLOCKER → error (legacy path)', () => {
      expect(getToastSeverity(makeTestMsg({ message_type: 'BLOCKER' }))).toBe('error');
    });

    it('operator-fyi → info even with BLOCKER message_type (audience is authoritative)', () => {
      expect(getToastSeverity(makeTestMsg({
        message_type: 'BLOCKER',
        context: { audience: 'operator-fyi' },
      }))).toBe('info');
    });
  });

  describe('isCriticalSeverity', () => {
    it('operator-action-needed → critical', () => {
      expect(isCriticalSeverity(makeTestMsg({ context: { audience: 'operator-action-needed' } }))).toBe(true);
    });

    it('strategist-internal → not critical even with BLOCKER message_type', () => {
      expect(isCriticalSeverity(makeTestMsg({
        message_type: 'BLOCKER',
        context: { audience: 'strategist-internal' },
      }))).toBe(false);
    });

    it('pipeline-log → not critical', () => {
      expect(isCriticalSeverity(makeTestMsg({ context: { audience: 'pipeline-log' } }))).toBe(false);
    });

    it('no audience QUESTION → critical (legacy path)', () => {
      expect(isCriticalSeverity(makeTestMsg({ message_type: 'QUESTION' }))).toBe(true);
    });

    it('operator-fyi → not critical even with BLOCKER message_type (audience is authoritative)', () => {
      expect(isCriticalSeverity(makeTestMsg({
        message_type: 'BLOCKER',
        context: { audience: 'operator-fyi' },
      }))).toBe(false);
    });
  });
});

// ── New event type classification coverage ─────────────────────────────────
// Tests added when operator_decision.summary_absent and notification.dead_letter_lost
// were added to ROUTINE_EVENT_LABELS. notification.dead_letter_lost has
// audience=operator-action-needed (not strategist-internal), making it a special
// case: the audience stamp routes modern messages as HIGH, while the
// STRATEGIST_INTERNAL_FALLBACK_BLOCK routes legacy (no-audience) messages as ROUTINE.

describe('new routine event types — operator_decision.summary_absent', () => {
  it('is in ROUTINE_EVENT_LABELS', () => {
    expect('operator_decision.summary_absent' in ROUTINE_EVENT_LABELS).toBe(true);
  });

  it('has human-readable label', () => {
    expect(ROUTINE_EVENT_LABELS['operator_decision.summary_absent']).toBeTruthy();
  });

  it('is not in HIGH_SIGNAL_NOTE_EVENTS', () => {
    expect(HIGH_SIGNAL_NOTE_EVENTS.has('operator_decision.summary_absent')).toBe(false);
  });

  it('is not in ESCALATION_EVENT_TYPES', () => {
    expect(ESCALATION_EVENT_TYPES.has('operator_decision.summary_absent')).toBe(false);
  });

  it('is in STRATEGIST_INTERNAL_FALLBACK_BLOCK (derived from ROUTINE_EVENT_LABELS keys)', () => {
    expect(STRATEGIST_INTERNAL_FALLBACK_BLOCK.has('operator_decision.summary_absent')).toBe(true);
  });

  it('with strategist-internal audience → routine (audience is authoritative)', () => {
    const classified = classifySignalLevel(makeTestMsg({
      context: { audience: 'strategist-internal', event_type: 'operator_decision.summary_absent' },
    }));
    expect(classified).toBe('routine');
  });

  it('without audience stamp → routine (STRATEGIST_INTERNAL_FALLBACK_BLOCK)', () => {
    const classified = classifySignalLevel(makeTestMsg({
      context: { event_type: 'operator_decision.summary_absent' },
    }));
    expect(classified).toBe('routine');
  });

  it('snapshot audience matches strategist-internal', () => {
    const entry = eventTypeSnapshot.event_types.find(
      e => e.name === 'operator_decision.summary_absent'
    );
    expect(entry).toBeDefined();
    expect(entry?.audience).toBe('strategist-internal');
  });
});

describe('new routine event types — notification.dead_letter_lost', () => {
  it('is in ROUTINE_EVENT_LABELS', () => {
    expect('notification.dead_letter_lost' in ROUTINE_EVENT_LABELS).toBe(true);
  });

  it('has human-readable label', () => {
    expect(ROUTINE_EVENT_LABELS['notification.dead_letter_lost']).toBeTruthy();
  });

  it('is not in HIGH_SIGNAL_NOTE_EVENTS', () => {
    // notification.dead_letter_lost has operator-action-needed audience but is NOT
    // in HIGH_SIGNAL_NOTE_EVENTS — it is in ROUTINE_EVENT_LABELS for display labeling.
    // Signal level is determined at runtime by the audience stamp, not this set.
    expect(HIGH_SIGNAL_NOTE_EVENTS.has('notification.dead_letter_lost')).toBe(false);
  });

  it('is not in ESCALATION_EVENT_TYPES', () => {
    expect(ESCALATION_EVENT_TYPES.has('notification.dead_letter_lost')).toBe(false);
  });

  it('is in STRATEGIST_INTERNAL_FALLBACK_BLOCK (derived from ROUTINE_EVENT_LABELS keys)', () => {
    // Even though its audience is operator-action-needed, the legacy fallback block
    // is derived from ROUTINE_EVENT_LABELS keys, so it appears here.
    expect(STRATEGIST_INTERNAL_FALLBACK_BLOCK.has('notification.dead_letter_lost')).toBe(true);
  });

  it('with operator-action-needed audience → high (audience is authoritative for modern messages)', () => {
    // notification.dead_letter_lost carries audience=operator-action-needed from the backend.
    // The audience stamp takes precedence over any fallback heuristic.
    const classified = classifySignalLevel(makeTestMsg({
      context: { audience: 'operator-action-needed', event_type: 'notification.dead_letter_lost' },
    }));
    expect(classified).toBe('high');
  });

  it('without audience stamp → routine (STRATEGIST_INTERNAL_FALLBACK_BLOCK prevents high promotion)', () => {
    // Legacy messages without an audience stamp: STRATEGIST_INTERNAL_FALLBACK_BLOCK
    // prevents notification.dead_letter_lost from being classified as high via
    // HIGH_SIGNAL_NOTE_EVENTS (it's not in that set anyway, but the block guards
    // against accidental future drift into HIGH_SIGNAL_NOTE_EVENTS).
    const classified = classifySignalLevel(makeTestMsg({
      context: { event_type: 'notification.dead_letter_lost' },
    }));
    expect(classified).toBe('routine');
  });

  it('with operator-action-needed audience → error toast (operator-facing)', () => {
    const severity = getToastSeverity(makeTestMsg({
      context: { audience: 'operator-action-needed', event_type: 'notification.dead_letter_lost' },
    }));
    expect(severity).toBe('error');
  });

  it('snapshot audience is operator-action-needed', () => {
    // Verifies that notification.dead_letter_lost is the special-case event type
    // that lives in ROUTINE_EVENT_LABELS but has operator-action-needed audience.
    const entry = eventTypeSnapshot.event_types.find(
      e => e.name === 'notification.dead_letter_lost'
    );
    expect(entry).toBeDefined();
    expect(entry?.audience).toBe('operator-action-needed');
  });
});

describe('ROUTINE_EVENT_LABELS and STRATEGIST_INTERNAL_FALLBACK_BLOCK parity', () => {
  it('STRATEGIST_INTERNAL_FALLBACK_BLOCK contains exactly the keys of ROUTINE_EVENT_LABELS', () => {
    const routineKeys = new Set(Object.keys(ROUTINE_EVENT_LABELS));
    for (const key of routineKeys) {
      expect(STRATEGIST_INTERNAL_FALLBACK_BLOCK.has(key)).toBe(true);
    }
    expect(STRATEGIST_INTERNAL_FALLBACK_BLOCK.size).toBe(routineKeys.size);
  });

  it('no ROUTINE_EVENT_LABELS key is in HIGH_SIGNAL_NOTE_EVENTS', () => {
    // Guards against the invariant described in the STRATEGIST_INTERNAL_FALLBACK_BLOCK
    // comment: STRATEGIST_INTERNAL_FALLBACK_BLOCK ∩ HIGH_SIGNAL_NOTE_EVENTS = ∅
    for (const key of Object.keys(ROUTINE_EVENT_LABELS)) {
      expect(HIGH_SIGNAL_NOTE_EVENTS.has(key)).toBe(false);
    }
  });
});
