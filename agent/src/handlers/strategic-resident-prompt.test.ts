import { describe, expect, it } from 'vitest';

import {
  STRATEGIC_RESIDENT_SYSTEM_PROMPT,
  STRATEGIC_RESIDENT_SYSTEM_PROMPT_MAX_CHARS,
} from './strategic-resident-prompt.js';

describe('STRATEGIC_RESIDENT_SYSTEM_PROMPT', () => {
  it('defines the Resident identity and strategic object model compactly', () => {
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain("project's long-lived strategic steward");
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('A direction is a durable strategic outcome');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('A task is one bounded unit');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('A running update is the replaceable live snapshot');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('A note preserves durable evidence');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('A problem-model gap explains why');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT.length).toBeLessThanOrEqual(
      STRATEGIC_RESIDENT_SYSTEM_PROMPT_MAX_CHARS,
    );
  });

  it('commissions implementation and forbids direction sharding', () => {
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('Do not implement the managed project yourself');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('commission implementation');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('active direction activates recurring pipeline attention');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('spawn multiple tasks under one direction');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('never create direction shards');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).not.toContain('dispatching BOTH');
  });

  it('preserves the native reply protocol', () => {
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('reply_to_operator tool exactly once');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('Normal assistant text is only a transcript');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('Never substitute ./messages send');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('OPERATOR-DECISION:');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('durable decisions register');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('send the substantive answer or plan first');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain('not heartbeats or routine cadence');
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain(
      'explicitly requests supported direct strategic operations',
    );
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain(
      'exact successful ./strategic response and a canonical ./fetch readback',
    );
    expect(STRATEGIC_RESIDENT_SYSTEM_PROMPT).toContain(
      'missing receipt, or mismatched readback is failure',
    );
  });
});
