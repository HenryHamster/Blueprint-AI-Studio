import { describe, it, expect } from 'vitest';
import {
  HIGH_SIGNAL_NOTE_EVENTS,
  CRITICAL_NOTE_EVENTS,
  ROUTINE_EVENT_LABELS,
  ESCALATION_EVENT_TYPES,
  ESCALATION_BADGE_LABELS,
  classifySignalLevel,
} from './MessagesPanel.utils';
import eventTypeSnapshot from '../../../backend/testing/event_type_snapshot.json';

/**
 * Backend↔Frontend Event Type Classification Invariant
 *
 * The backend emits 21 distinct event_type values in notification_dispatch.py.
 * The frontend classifies these into HIGH_SIGNAL_NOTE_EVENTS, CRITICAL_NOTE_EVENTS,
 * ESCALATION_EVENT_TYPES, and ROUTINE_EVENT_LABELS.
 *
 * This test ensures:
 * 1. Every event_type from the backend is classified by the frontend
 * 2. Every event_type is in either HIGH_SIGNAL_NOTE_EVENTS or ROUTINE_EVENT_LABELS
 * 3. Every escalation event_type has a badge label
 * 4. Classifications align with the snapshot's intent (escalation → high-signal,
 *    high_signal → high-signal, routine → routine)
 *
 * When this test fails, the error message explains which event_type is missing
 * and suggests which classification set it should join (based on the snapshot).
 * Do NOT auto-classify — the snapshot classification is a best-guess from context;
 * the human must verify.
 *
 * See: backend/testing/event_type_snapshot.json for the canonical list.
 * To add a new event_type:
 * 1. Update backend/testing/event_type_snapshot.json with the new entry (name, classification, source_fn)
 * 2. This test will fail and tell you exactly where to add the label in this file
 */

describe('event-type classification invariant', () => {
  const snapshotEventTypes = eventTypeSnapshot.event_types;

  it('all backend event_types have frontend coverage', () => {
    const uncovered: string[] = [];
    const missingLabels: Array<{ event_type: string; classification: string }> = [];

    for (const entry of snapshotEventTypes) {
      const { name: eventType, classification } = entry;

      // Check if event_type appears in any frontend classification
      const isInHighSignal = HIGH_SIGNAL_NOTE_EVENTS.has(eventType);
      const isInRoutine = ROUTINE_EVENT_LABELS[eventType] !== undefined;
      const isInEscalation = ESCALATION_EVENT_TYPES.has(eventType);

      if (!isInHighSignal && !isInRoutine && !isInEscalation) {
        uncovered.push(eventType);
      }

      // Check that escalation event_types have badge labels
      if (classification === 'escalation' && !ESCALATION_BADGE_LABELS[eventType]) {
        missingLabels.push({ event_type: eventType, classification });
      }
    }

    const errorParts: string[] = [];

    if (uncovered.length > 0) {
      errorParts.push(
        `Uncovered event_types (not in HIGH_SIGNAL_NOTE_EVENTS, ROUTINE_EVENT_LABELS, or ESCALATION_EVENT_TYPES):\n` +
        uncovered.map(et => {
          const entry = snapshotEventTypes.find(e => e.name === et);
          const suggestion =
            entry?.classification === 'escalation'
              ? 'ESCALATION_EVENT_TYPES and ESCALATION_BADGE_LABELS'
              : entry?.classification === 'high_signal'
                ? 'HIGH_SIGNAL_NOTE_EVENTS'
                : 'ROUTINE_EVENT_LABELS';
          return `  • ${et} (suggest: ${suggestion})`;
        }).join('\n')
      );
    }

    if (missingLabels.length > 0) {
      errorParts.push(
        `Escalation event_types missing badge labels in ESCALATION_BADGE_LABELS:\n` +
        missingLabels.map(({ event_type }) => `  • ${event_type}`).join('\n')
      );
    }

    if (errorParts.length > 0) {
      throw new Error(
        'Event type classification drift detected:\n\n' +
        errorParts.join('\n\n') +
        '\n\n' +
        'Update MessagesPanel.utils.ts to add missing event_type classifications.\n' +
        'See backend/testing/event_type_snapshot.json for canonical list.'
      );
    }
  });

  it('escalation event_types are high-signal', () => {
    for (const et of ESCALATION_EVENT_TYPES) {
      expect(HIGH_SIGNAL_NOTE_EVENTS.has(et)).toBe(true);
    }
  });

  it('escalation event_types are critical', () => {
    for (const et of ESCALATION_EVENT_TYPES) {
      expect(CRITICAL_NOTE_EVENTS.has(et)).toBe(true);
    }
  });

  it('high_signal event_types (non-escalation) are high-signal', () => {
    const highSignalNonEscalation = Array.from(HIGH_SIGNAL_NOTE_EVENTS).filter(
      et => !ESCALATION_EVENT_TYPES.has(et)
    );
    for (const et of highSignalNonEscalation) {
      expect(HIGH_SIGNAL_NOTE_EVENTS.has(et)).toBe(true);
      const classified = classifySignalLevel({
        from_type: 'system',
        message_type: 'NOTE',
        context: { event_type: et },
      });
      expect(classified).toBe('high');
    }
  });

  it('routine event_types remain routine-signal unless manually surfaced', () => {
    for (const eventType of Object.keys(ROUTINE_EVENT_LABELS)) {
      // Routine event_types should classify as routine when no preference is set
      const classified = classifySignalLevel({
        from_type: 'system',
        message_type: 'NOTE',
        context: { event_type: eventType },
      });
      if (!HIGH_SIGNAL_NOTE_EVENTS.has(eventType)) {
        expect(classified).toBe('routine');
      }
    }
  });

  it('snapshot classifications align with frontend behavior', () => {
    for (const entry of snapshotEventTypes) {
      const { name: eventType, classification } = entry;

      switch (classification) {
        case 'escalation':
          expect(ESCALATION_EVENT_TYPES.has(eventType)).toBe(true);
          expect(ESCALATION_BADGE_LABELS[eventType]).toBeTruthy();
          break;

        case 'high_signal':
          expect(HIGH_SIGNAL_NOTE_EVENTS.has(eventType)).toBe(true);
          // high_signal is high-signal but may not be escalation
          expect(ESCALATION_EVENT_TYPES.has(eventType)).toBe(false);
          break;

        case 'routine':
          expect(ROUTINE_EVENT_LABELS[eventType]).toBeTruthy();
          expect(HIGH_SIGNAL_NOTE_EVENTS.has(eventType)).toBe(false);
          expect(ESCALATION_EVENT_TYPES.has(eventType)).toBe(false);
          break;

        default:
          throw new Error(`Unknown classification: ${classification}`);
      }
    }
  });
});
