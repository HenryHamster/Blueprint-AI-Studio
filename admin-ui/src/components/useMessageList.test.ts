import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useMessageList, type UseMessageListInput } from './useMessageList';
import type { ProjectMessage } from './MessageBubble';

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'm1', project_id: 'p1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: '2026-05-29T12:00:00Z',
    ...overrides,
  };
}

function baseInput(overrides: Partial<UseMessageListInput> = {}): UseMessageListInput {
  return {
    messages: [],
    eventPrefs: {},
    tempMutes: {},
    muteWindow: null,
    searchQuery: '',
    sortMode: 'importance',
    criticalOnly: false,
    showResolved: false,
    showOnlyNew: false,
    dividerTime: null,
    ...overrides,
  };
}

function run(overrides: Partial<UseMessageListInput> = {}) {
  return renderHook(() => useMessageList(baseInput(overrides))).result.current;
}

describe('useMessageList', () => {
  it('splits open messages into high-signal vs routine', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const routine = makeMsg({ id: 'r', message_type: 'NOTE', context: { event_type: 'task_started' } });
    const res = run({ messages: [blocker, routine] });
    expect(res.highSignalOpen.map(m => m.id)).toEqual(['b']);
    expect(res.routineOpen.map(m => m.id)).toEqual(['r']);
    expect(res.openMessages).toHaveLength(2);
  });

  it('pins BLOCKERs and QUESTIONs separately in importance mode', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const question = makeMsg({ id: 'q', message_type: 'QUESTION' });
    const feedback = makeMsg({ id: 'f', message_type: 'FEEDBACK' });
    const res = run({ messages: [blocker, question, feedback], sortMode: 'importance' });
    expect(res.pinnedBlockers.map(m => m.id)).toEqual(['b']);
    expect(res.pinnedQuestions.map(m => m.id)).toEqual(['q']);
    expect(res.otherHighSignal.map(m => m.id)).toEqual(['f']);
  });

  it('does not pin in recent mode (all high-signal go to otherHighSignal)', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const res = run({ messages: [blocker], sortMode: 'recent' });
    expect(res.pinnedBlockers).toHaveLength(0);
    expect(res.otherHighSignal.map(m => m.id)).toEqual(['b']);
  });

  it('auto-promotes failing routine groups', () => {
    const grp = [0, 1, 2].map(i => makeMsg({
      id: `g${i}`,
      message_type: 'NOTE',
      context: { event_type: 'ci_run', result: 'failed' },
      created_at: `2026-05-29T12:0${i}:00Z`,
    }));
    const res = run({ messages: grp });
    expect(res.promotedGroups).toHaveLength(1);
    expect(res.promotedGroups[0].msgs).toHaveLength(3);
    // promoted group is removed from the remaining routine rows
    expect(res.remainingRoutineRows.filter(r => r.kind === 'group')).toHaveLength(0);
  });

  it('filters routine from remaining section while keeping promoted groups when mute is active', () => {
    const grp = [0, 1, 2].map(i => makeMsg({
      id: `g${i}`,
      message_type: 'NOTE',
      context: { event_type: 'ci_run', result: 'failed' },
      created_at: `2026-05-29T12:0${i}:00Z`,
    }));
    const single = makeMsg({ id: 's', message_type: 'NOTE', context: { event_type: 'task_done' } });
    const muteWindow = { expiresAt: Date.now() + 3_600_000 };
    const res = run({ messages: [...grp, single], muteWindow });
    expect(res.muteActive).toBe(true);
    expect(res.promotedGroups).toHaveLength(1);
    // under active mute, remaining keeps only promoted groups; the lone non-promoted
    // routine single is filtered out
    expect(res.remainingRoutineRows.every(r => r.kind === 'group')).toBe(true);
    expect(res.remainingRoutineRows.some(r => r.kind === 'single')).toBe(false);
    expect(res.visibleRoutineCount).toBe(3);
    expect(res.mutedRoutineCount).toBe(1);
  });

  it('applies search filtering across high-signal and routine', () => {
    const a = makeMsg({ id: 'a', message_type: 'BLOCKER', content: 'database outage' });
    const b = makeMsg({ id: 'b', message_type: 'BLOCKER', content: 'unrelated note' });
    const res = run({ messages: [a, b], searchQuery: 'database' });
    expect(res.highSignalOpen.map(m => m.id)).toEqual(['a']);
  });

  it('slices visibleResolved to the most recent 5 unless showResolved', () => {
    const resolved = Array.from({ length: 8 }, (_, i) => makeMsg({
      id: `res${i}`,
      status: 'resolved',
      created_at: `2026-05-2${i}T12:00:00Z`,
    }));
    const collapsed = run({ messages: resolved, showResolved: false });
    expect(collapsed.visibleResolved).toHaveLength(5);
    expect(collapsed.hiddenResolvedCount).toBe(3);

    const expanded = run({ messages: resolved, showResolved: true });
    expect(expanded.visibleResolved).toHaveLength(8);
    expect(expanded.hiddenResolvedCount).toBe(0);
  });

  it('computes newMessageCounts and totalNewCount relative to dividerTime', () => {
    const dividerTime = Date.parse('2026-05-29T12:00:00Z');
    const newBlocker = makeMsg({ id: 'nb', message_type: 'BLOCKER', created_at: '2026-05-29T13:00:00Z' });
    const oldBlocker = makeMsg({ id: 'ob', message_type: 'BLOCKER', created_at: '2026-05-29T11:00:00Z' });
    const newRoutine = makeMsg({ id: 'nr', message_type: 'NOTE', context: { event_type: 'task_started' }, created_at: '2026-05-29T13:30:00Z' });
    const res = run({ messages: [newBlocker, oldBlocker, newRoutine], dividerTime });
    expect(res.newMessageCounts.blockers).toBe(1);
    expect(res.newMessageCounts.routine).toBe(1);
    expect(res.newHighSignalCount).toBe(1);
    expect(res.totalNewCount).toBe(2);
  });

  it('excludes non-critical messages when criticalOnly is set', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const feedback = makeMsg({ id: 'f', message_type: 'FEEDBACK' });
    const res = run({ messages: [blocker, feedback], criticalOnly: true });
    // FEEDBACK is not critical severity; only BLOCKER survives the open filter
    expect(res.openMessages.map(m => m.id)).toEqual(['b']);
  });

  it('needsActionOnly=true narrows highSignalOpen to needsOperatorIds only and empties routineOpen', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const needsOp = makeMsg({ id: 'na', message_type: 'NOTE', from_type: 'system', context: { event_type: 'failure_alert' } });
    const routine = makeMsg({ id: 'r', message_type: 'NOTE', context: { event_type: 'task_started' } });
    const needsOperatorIds = new Set(['na']);

    const res = run({ messages: [blocker, needsOp, routine], needsOperatorIds, needsActionOnly: true });
    expect(res.highSignalOpen.map(m => m.id)).toEqual(['na']);
    expect(res.routineOpen).toHaveLength(0);
  });

  it('needsActionOnly=true with no needsOperatorIds falls through to normal behavior', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const routine = makeMsg({ id: 'r', message_type: 'NOTE', context: { event_type: 'task_started' } });

    const res = run({ messages: [blocker, routine], needsActionOnly: true });
    // needsOperatorIds is undefined → falls through to normal signal-based split
    expect(res.highSignalOpen.map(m => m.id)).toEqual(['b']);
    expect(res.routineOpen.map(m => m.id)).toEqual(['r']);
  });

  it('needsActionOnly=false shows all messages regardless of needsOperatorIds', () => {
    const blocker = makeMsg({ id: 'b', message_type: 'BLOCKER' });
    const needsOp = makeMsg({ id: 'na', message_type: 'NOTE', from_type: 'system', context: { event_type: 'failure_alert' } });
    const routine = makeMsg({ id: 'r', message_type: 'NOTE', context: { event_type: 'task_started' } });
    const needsOperatorIds = new Set(['na']);

    const res = run({ messages: [blocker, needsOp, routine], needsOperatorIds, needsActionOnly: false });
    // Full signal-based split: blocker and failure_alert go high-signal, task_started goes routine
    expect(res.highSignalOpen.map(m => m.id).sort()).toEqual(['b', 'na'].sort());
    expect(res.routineOpen.map(m => m.id)).toEqual(['r']);
  });

  it('promotes needs_operator attention item to high-signal even when event_type is muted', () => {
    // failure_alert is high-signal by default; muting it demotes it to routine
    const alert = makeMsg({
      id: 'fa1',
      message_type: 'NOTE',
      from_type: 'system',
      context: { event_type: 'failure_alert' },
    });
    const eventPrefs = { failure_alert: 'mute' as const };
    // Without needsOperatorIds the muted item goes to routine, not high-signal
    const withoutAttention = run({ messages: [alert], eventPrefs });
    expect(withoutAttention.highSignalOpen.map(m => m.id)).toEqual([]);
    expect(withoutAttention.routineOpen.map(m => m.id)).toEqual([]);

    // With the canonical attention model marking it needs_operator, it is
    // promoted to highSignalOpen and excluded from routineOpen.
    const withAttention = run({
      messages: [alert],
      eventPrefs,
      needsOperatorIds: new Set(['fa1']),
    });
    expect(withAttention.highSignalOpen.map(m => m.id)).toEqual(['fa1']);
    expect(withAttention.routineOpen.map(m => m.id)).toEqual([]);
  });
});
