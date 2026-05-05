import { describe, it, expect, beforeEach } from 'vitest';
import {
  pushFixLoopEvent,
  drainFixLoopEventBuffer,
  requeueFixLoopEvents,
  resetCheckSynthesizerState,
  synthesizeCheckFixLoopEvents,
  fingerprintHash,
  isCheckInvocation,
  recordCheckOutcome,
  getCheckRunawayStatus,
  _resetFixLoopEventStateForTesting,
  _peekFixLoopEventBufferForTesting,
  _peekCheckOutcomesForTesting,
  type FixLoopEvent,
  type ToolTraceEntryLike,
} from './fix-loop-events.js';

beforeEach(() => {
  _resetFixLoopEventStateForTesting();
});

describe('fingerprintHash', () => {
  it('returns a stable 16-hex-char digest', () => {
    const a = fingerprintHash('lint failed: foo.ts:1:1');
    const b = fingerprintHash('lint failed: foo.ts:1:1');
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f]{16}$/);
  });

  it('differs for different inputs', () => {
    expect(fingerprintHash('a')).not.toBe(fingerprintHash('b'));
  });
});

describe('isCheckInvocation', () => {
  it('matches ./check with trailing space, end-of-string, or arg', () => {
    expect(isCheckInvocation('{"command":"./check"}')).toBe(true);
    expect(isCheckInvocation('{"command":"./check test"}')).toBe(true);
    expect(isCheckInvocation('{"command":"./check ","description":"x"}')).toBe(true);
  });

  it('rejects other ./check-prefixed commands', () => {
    expect(isCheckInvocation('{"command":"./checker"}')).toBe(false);
    expect(isCheckInvocation('{"command":"./check-something"}')).toBe(false);
    expect(isCheckInvocation('{"command":"npm test"}')).toBe(false);
  });
});

describe('fix-loop event buffer', () => {
  it('drains all queued events FIFO and clears the buffer', () => {
    pushFixLoopEvent({
      loop: 'lint', iteration: 1, fingerprint_hash: 'a', error_tail: 'e',
      change_summary: '', exit_reason: 'in_progress', source: 'agent_emitted',
      started_at: '2026-05-04T00:00:00Z', ended_at: '2026-05-04T00:00:01Z',
    });
    pushFixLoopEvent({
      loop: 'lint', iteration: 2, fingerprint_hash: 'b', error_tail: 'e',
      change_summary: '', exit_reason: 'passed', source: 'agent_emitted',
      started_at: '2026-05-04T00:00:02Z', ended_at: '2026-05-04T00:00:03Z',
    });
    const drained = drainFixLoopEventBuffer();
    expect(drained.map(e => e.iteration)).toEqual([1, 2]);
    expect(_peekFixLoopEventBufferForTesting()).toEqual([]);
    // Second drain returns empty array, not error.
    expect(drainFixLoopEventBuffer()).toEqual([]);
  });

  it('caps buffer at 20 events, dropping oldest first', () => {
    for (let i = 1; i <= 25; i++) {
      pushFixLoopEvent({
        loop: 'lint', iteration: i, fingerprint_hash: 'h', error_tail: '',
        change_summary: '', exit_reason: 'in_progress', source: 'agent_emitted',
        started_at: 'x', ended_at: 'y',
      });
    }
    const drained = drainFixLoopEventBuffer();
    expect(drained.length).toBe(20);
    expect(drained[0].iteration).toBe(6);
    expect(drained[drained.length - 1].iteration).toBe(25);
  });
});

describe('synthesizeCheckFixLoopEvents', () => {
  function entry(t: string, tool: string, preview: string): ToolTraceEntryLike {
    return { t, tool, input_preview: preview };
  }

  it('emits one check event per ./check invocation in the trace', () => {
    const trace: ToolTraceEntryLike[] = [
      entry('2026-05-04T00:00:00Z', 'Edit', '{"file_path":"a.ts"}'),
      entry('2026-05-04T00:00:01Z', 'Bash', '{"command":"./check"}'),
      entry('2026-05-04T00:00:02Z', 'Edit', '{"file_path":"a.ts"}'),
      entry('2026-05-04T00:00:03Z', 'Edit', '{"file_path":"b.ts"}'),
      entry('2026-05-04T00:00:04Z', 'Bash', '{"command":"./check"}'),
    ];
    const events = synthesizeCheckFixLoopEvents(trace);
    expect(events.length).toBe(2);
    expect(events[0]).toMatchObject({
      loop: 'check', iteration: 1, source: 'trace_synthesized',
      exit_reason: 'in_progress',
    });
    expect(events[1]).toMatchObject({
      loop: 'check', iteration: 2, source: 'trace_synthesized',
    });
    // change_summary for the second invocation summarizes intervening tools.
    expect(events[1].change_summary).toContain('Edit:2');
  });

  it('does not re-emit invocations seen on a prior walk', () => {
    const traceFirst: ToolTraceEntryLike[] = [
      entry('2026-05-04T00:00:01Z', 'Bash', '{"command":"./check"}'),
    ];
    const first = synthesizeCheckFixLoopEvents(traceFirst);
    expect(first.length).toBe(1);
    const second = synthesizeCheckFixLoopEvents(traceFirst);
    expect(second.length).toBe(0);

    // A new ./check appearing later should keep iteration monotonic.
    const traceSecond: ToolTraceEntryLike[] = [
      ...traceFirst,
      entry('2026-05-04T00:00:02Z', 'Bash', '{"command":"./check"}'),
    ];
    const third = synthesizeCheckFixLoopEvents(traceSecond);
    expect(third.length).toBe(1);
    expect(third[0].iteration).toBe(2);
  });

  it('returns no events when the trace contains no ./check invocations', () => {
    const trace: ToolTraceEntryLike[] = [
      entry('2026-05-04T00:00:00Z', 'Read', '{"file_path":"x"}'),
      entry('2026-05-04T00:00:01Z', 'Bash', '{"command":"git status"}'),
    ];
    expect(synthesizeCheckFixLoopEvents(trace)).toEqual([]);
  });

  it('returns empty for an empty trace without erroring', () => {
    expect(synthesizeCheckFixLoopEvents([])).toEqual([]);
  });
});

describe('resetCheckSynthesizerState', () => {
  it('resets iteration counter and watermarks so next walk starts at 1', () => {
    const trace: ToolTraceEntryLike[] = [
      { t: '2026-05-04T00:00:01Z', tool: 'Bash', input_preview: '{"command":"./check"}' },
      { t: '2026-05-04T00:00:02Z', tool: 'Bash', input_preview: '{"command":"./check"}' },
    ];
    const first = synthesizeCheckFixLoopEvents(trace);
    expect(first.length).toBe(2);
    expect(first[1].iteration).toBe(2);

    // Without a reset, the next role's first ./check would be iteration 3.
    resetCheckSynthesizerState();

    // Simulate the next role's tool trace starting fresh.
    const nextRoleTrace: ToolTraceEntryLike[] = [
      { t: '2026-05-04T01:00:00Z', tool: 'Bash', input_preview: '{"command":"./check"}' },
    ];
    const next = synthesizeCheckFixLoopEvents(nextRoleTrace);
    expect(next.length).toBe(1);
    expect(next[0].iteration).toBe(1);
    // started_at should reflect the new role's trace, not the prior role's
    // last ended_at.
    expect(next[0].started_at).toBe('2026-05-04T01:00:00Z');
  });
});

describe('requeueFixLoopEvents', () => {
  function ev(i: number): FixLoopEvent {
    return {
      loop: 'lint', iteration: i, fingerprint_hash: 'h', error_tail: '',
      change_summary: '', exit_reason: 'in_progress', source: 'agent_emitted',
      started_at: 'x', ended_at: 'y',
    };
  }

  it('restores drained events to the head, preserving FIFO on next drain', () => {
    pushFixLoopEvent(ev(3));
    requeueFixLoopEvents([ev(1), ev(2)]);
    const drained = drainFixLoopEventBuffer();
    expect(drained.map(e => e.iteration)).toEqual([1, 2, 3]);
  });

  it('respects the buffer cap, dropping the oldest re-queued events first', () => {
    // Fill the buffer with 15 newer events first.
    for (let i = 100; i < 115; i++) pushFixLoopEvent(ev(i));
    // Re-queue 10 older events — total would be 25, must cap at 20 by
    // dropping the oldest (i.e. the head of the re-queued batch).
    requeueFixLoopEvents([ev(1), ev(2), ev(3), ev(4), ev(5), ev(6), ev(7), ev(8), ev(9), ev(10)]);
    const drained = drainFixLoopEventBuffer();
    expect(drained.length).toBe(20);
    // The oldest 5 of the re-queued batch were dropped; the remaining 5
    // re-queued events come before the 15 that were already in the buffer.
    expect(drained[0].iteration).toBe(6);
    expect(drained[4].iteration).toBe(10);
    expect(drained[5].iteration).toBe(100);
    expect(drained[drained.length - 1].iteration).toBe(114);
  });

  it('is a no-op for empty input', () => {
    pushFixLoopEvent(ev(1));
    requeueFixLoopEvents([]);
    expect(_peekFixLoopEventBufferForTesting().map(e => e.iteration)).toEqual([1]);
  });
});

describe('recordCheckOutcome / getCheckRunawayStatus', () => {
  it('accumulates outcomes in order', () => {
    recordCheckOutcome(true);
    recordCheckOutcome(false);
    recordCheckOutcome(true);
    expect(_peekCheckOutcomesForTesting()).toEqual([true, false, true]);
  });

  it('returns shouldTrigger=false when below minInvocations', () => {
    // 5 calls (all failing) but min is 6
    for (let i = 0; i < 5; i++) recordCheckOutcome(false);
    const { totalChecks, shouldTrigger } = getCheckRunawayStatus(6, 3);
    expect(totalChecks).toBe(5);
    expect(shouldTrigger).toBe(false);
  });

  it('returns shouldTrigger=true when ≥minInvocations and last N all failed', () => {
    recordCheckOutcome(true);   // pass
    recordCheckOutcome(false);  // fail
    recordCheckOutcome(false);  // fail
    recordCheckOutcome(false);  // fail — last 3 all fail, total=4 < 6
    expect(getCheckRunawayStatus(6, 3).shouldTrigger).toBe(false);
    // push 2 more fails to reach minInvocations=6
    recordCheckOutcome(false);
    recordCheckOutcome(false);
    const { totalChecks, shouldTrigger } = getCheckRunawayStatus(6, 3);
    expect(totalChecks).toBe(6);
    expect(shouldTrigger).toBe(true);
  });

  it('returns shouldTrigger=false if last N includes a pass even at minInvocations', () => {
    for (let i = 0; i < 5; i++) recordCheckOutcome(false);
    recordCheckOutcome(true);  // most recent: pass
    const { shouldTrigger } = getCheckRunawayStatus(6, 3);
    expect(shouldTrigger).toBe(false);
  });

  it('returns shouldTrigger=false for an empty outcome list', () => {
    const { totalChecks, shouldTrigger } = getCheckRunawayStatus(6, 3);
    expect(totalChecks).toBe(0);
    expect(shouldTrigger).toBe(false);
  });

  it('resetCheckSynthesizerState clears the outcome buffer', () => {
    recordCheckOutcome(false);
    recordCheckOutcome(false);
    expect(_peekCheckOutcomesForTesting().length).toBe(2);
    resetCheckSynthesizerState();
    expect(_peekCheckOutcomesForTesting().length).toBe(0);
    expect(getCheckRunawayStatus(6, 3).totalChecks).toBe(0);
  });
});
