import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesListView, type MessagesListViewProps, buildTriageItems, type BuildParams } from './MessagesListView';
import type { ProjectMessage } from './MessageBubble';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt, stressStrings } from '../test/viewport';

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'msg-1', project_id: 'proj-1', message_type: 'NOTE', from_type: 'system',
    from_task_id: null, content: 'viewport list content', context: {}, status: 'open',
    resolution: null, resolved_at: null, created_at: new Date().toISOString(),
    ...overrides,
  };
}

const bubbleProps: MessagesListViewProps['bubbleProps'] = {
  resolvingId: null,
  resolveInput: '',
  deletingIds: new Set(),
  creatingDirectionIds: new Set(),
  investigatingId: null,
  investigatingIds: new Set(),
  selectMode: false,
  selectedIds: new Set(),
  onOpenTask: undefined,
  onToggleSelect: () => { /* no-op */ },
  onCreateDirection: () => { /* no-op */ },
  onCreateInvestigation: () => { /* no-op */ },
  onResolve: () => { /* no-op */ },
  onDelete: () => { /* no-op */ },
  onStartResolve: () => { /* no-op */ },
  onCancelResolve: () => { /* no-op */ },
  onSetResolveInput: () => { /* no-op */ },
  onStartInvestigate: () => { /* no-op */ },
  onCancelInvestigate: () => { /* no-op */ },
  onReply: () => { /* no-op */ },
};

function baseProps(overrides: Partial<MessagesListViewProps> = {}): MessagesListViewProps {
  return {
    scrollRef: createRef<HTMLDivElement>(),
    dividerDismissedRef: { current: false },
    messages: [],
    loading: false,
    dividerTime: null,
    totalNewCount: 0,
    newMessageCounts: { blockers: 0, questions: 0, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0 },
    routineOpen: [],
    linkedMessages: [],
    pinnedBlockers: [],
    pinnedQuestions: [],
    otherHighSignal: [],
    promotedGroups: [],
    remainingRoutineRows: [],
    autoExpandedKeys: new Set(),
    visibleRoutineCount: 0,
    mutedRoutineCount: 0,
    visibleResolved: [],
    hiddenResolvedCount: 0,
    resolvedMessages: [],
    searchQuery: '',
    showOnlyNew: false,
    showResolved: false,
    routineExpanded: false,
    showScrollDown: false,
    muteActive: false,
    showTempMuteMenu: false,
    expandedGroupKeys: new Set(),
    tempMutes: {},
    bubbleProps,
    onDismissDivider: () => { /* no-op */ },
    onShowOnlyNew: () => { /* no-op */ },
    setExpandedGroupKeys: () => { /* no-op */ },
    setRoutineExpanded: () => { /* no-op */ },
    setShowResolved: () => { /* no-op */ },
    setShowTempMuteMenu: () => { /* no-op */ },
    addTemporaryMute: () => { /* no-op */ },
    getTop3NoisyEventTypes: () => [],
    handleResolveGroup: () => { /* no-op */ },
    handleBulkDeleteResolved: () => { /* no-op */ },
    scrollToBottom: () => { /* no-op */ },
    ...overrides,
  };
}

describe('MessagesListView — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  async function renderView(props: MessagesListViewProps) {
    host = document.createElement('div');
    // Mirror the panel's flex-column shell so the flex-1 scroll area is width-constrained.
    // Height is bounded so the virtualized list renders rows (unbounded height → zero rows).
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.width = '375px';
    host.style.height = '600px';
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(<MessagesListView {...props} />);
    });
  }

  test('empty state has no horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await renderView(baseProps());
    });
  });

  test('populated list has no horizontal overflow at 375px', async () => {
    const high = makeMsg({ id: 'b1', message_type: 'BLOCKER', content: stressStrings.title });
    const other = makeMsg({ id: 'n1', message_type: 'NOTE', content: stressStrings.directionText });
    await expectNoOverflowAt(375, async () => {
      await renderView(baseProps({
        messages: [high, other],
        pinnedBlockers: [high],
        otherHighSignal: [other],
      }));
    });
  });

  test('populated list has no silent text truncation at 375px', async () => {
    const other = makeMsg({ id: 'n1', message_type: 'NOTE', content: stressStrings.directionText });
    await expectNoTextTruncationAt(375, async () => {
      await renderView(baseProps({ messages: [other], otherHighSignal: [other] }));
    });
  });

  test('action buttons meet minimum touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await renderView(baseProps({ showScrollDown: true, hiddenResolvedCount: 3 }));
    });
  });
});

// ─── buildTriageItems unit tests ──────────────────────────────────────────────

function makeParams(overrides: Partial<BuildParams> = {}): BuildParams {
  return {
    loading: false,
    messages: [],
    dividerTime: null,
    totalNewCount: 0,
    dividerDismissedCurrent: false,
    showOnlyNew: false,
    showResolved: false,
    routineExpanded: false,
    muteActive: false,
    searchQuery: '',
    pinnedBlockers: [],
    pinnedQuestions: [],
    otherHighSignal: [],
    promotedGroups: [],
    remainingRoutineRows: [],
    autoExpandedKeys: new Set(),
    expandedGroupKeys: new Set(),
    routineOpen: [],
    linkedMessages: [],
    visibleResolved: [],
    hiddenResolvedCount: 0,
    resolvedMessages: [],
    newMessageCounts: { blockers: 0, questions: 0, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0 },
    ...overrides,
  };
}

describe('buildTriageItems — section indices and item ordering', () => {
  test('empty messages → no_messages item, all section indices -1', () => {
    const { items, sectionIndices } = buildTriageItems(makeParams());
    expect(items).toEqual([{ kind: 'empty', emptyKind: 'no_messages' }]);
    expect(sectionIndices).toEqual({ blockers: -1, questions: -1, routine: -1, newDivider: -1 });
  });

  test('loading → loading item, all section indices -1', () => {
    const msg = makeMsg();
    const { items, sectionIndices } = buildTriageItems(makeParams({ loading: true, messages: [msg] }));
    expect(items[0]).toEqual({ kind: 'loading' });
    expect(sectionIndices).toEqual({ blockers: -1, questions: -1, routine: -1, newDivider: -1 });
  });

  test('showOnlyNew with dividerTime and nothing visible → no_new', () => {
    const msg = makeMsg();
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      showOnlyNew: true,
      dividerTime: 1000,
    }));
    expect(items[0]).toEqual({ kind: 'empty', emptyKind: 'no_new' });
  });

  test('searchQuery with nothing matching → no_match', () => {
    const msg = makeMsg();
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      searchQuery: 'zzz-no-match',
    }));
    expect(items[0]).toEqual({ kind: 'empty', emptyKind: 'no_match' });
  });

  test('pinned blockers → blockers index set, questions and routine -1', () => {
    const msg = makeMsg({ id: 'b1', message_type: 'BLOCKER' });
    const { items, sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      pinnedBlockers: [msg],
    }));
    expect(sectionIndices.blockers).toBeGreaterThanOrEqual(0);
    expect(items[sectionIndices.blockers]).toEqual({ kind: 'section_head_blockers' });
    expect(sectionIndices.questions).toBe(-1);
    expect(sectionIndices.routine).toBe(-1);
    expect(sectionIndices.newDivider).toBe(-1);
  });

  test('pinned questions → questions index set, blockers -1', () => {
    const msg = makeMsg({ id: 'q1', message_type: 'QUESTION' });
    const { items, sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      pinnedQuestions: [msg],
    }));
    expect(sectionIndices.blockers).toBe(-1);
    expect(sectionIndices.questions).toBeGreaterThanOrEqual(0);
    expect(items[sectionIndices.questions].kind).toBe('section_head_questions');
    expect(sectionIndices.newDivider).toBe(-1);
  });

  test('blockers before questions in item order', () => {
    const blocker = makeMsg({ id: 'b1', message_type: 'BLOCKER' });
    const question = makeMsg({ id: 'q1', message_type: 'QUESTION' });
    const { sectionIndices } = buildTriageItems(makeParams({
      messages: [blocker, question],
      pinnedBlockers: [blocker],
      pinnedQuestions: [question],
    }));
    expect(sectionIndices.blockers).toBeGreaterThanOrEqual(0);
    expect(sectionIndices.questions).toBeGreaterThan(sectionIndices.blockers);
  });

  test('new divider index set when dividerTime, totalNewCount > 0, not dismissed', () => {
    const msg = makeMsg({ id: 'n1' });
    const { items, sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      otherHighSignal: [msg],
      dividerTime: 1000,
      totalNewCount: 1,
      dividerDismissedCurrent: false,
    }));
    expect(sectionIndices.newDivider).toBeGreaterThanOrEqual(0);
    expect(items[sectionIndices.newDivider]).toEqual({ kind: 'new_divider' });
  });

  test('new divider not emitted when divider dismissed', () => {
    const msg = makeMsg({ id: 'n1' });
    const { sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      otherHighSignal: [msg],
      dividerTime: 1000,
      totalNewCount: 1,
      dividerDismissedCurrent: true,
    }));
    expect(sectionIndices.newDivider).toBe(-1);
  });

  test('routine section index set when routineOpen is non-empty', () => {
    const msg = makeMsg({ id: 'r1' });
    const { items, sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      routineOpen: [msg],
    }));
    expect(sectionIndices.routine).toBeGreaterThanOrEqual(0);
    expect(items[sectionIndices.routine]).toEqual({ kind: 'routine_head' });
  });

  test('routine section index -1 when routineOpen is empty', () => {
    const msg = makeMsg({ id: 'n1' });
    const { sectionIndices } = buildTriageItems(makeParams({
      messages: [msg],
      otherHighSignal: [msg],
    }));
    expect(sectionIndices.routine).toBe(-1);
  });
});
