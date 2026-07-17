import { describe, it, expect } from 'vitest';
import {
  HIGH_SIGNAL_NOTE_EVENTS,
  ESCALATION_EVENT_TYPES,
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
