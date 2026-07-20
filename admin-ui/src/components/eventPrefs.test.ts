import { describe, it, expect } from 'vitest';
import {
  HIGH_SIGNAL_NOTE_EVENTS,
  ESCALATION_EVENT_TYPES,
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
