import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesListView, type MessagesListViewProps, type ConversationListImperativeApi, buildTriageItems, type BuildParams } from './MessagesListView';
import { preserveScrollAnchorAfterPrepend } from '../utils/conversationScrollAnchor';
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
  onDelete: () => { /* no-op */ },
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
    handleBulkDeleteResolved: () => { /* no-op */ },
    scrollToBottom: () => { /* no-op */ },
    ...overrides,
  };
}

test('preserves the visible scroll anchor when older conversation rows prepend', async () => {
  const viewport = { scrollHeight: 1_000, scrollTop: 320 };
  let scheduled: (() => void) | null = null;
  await preserveScrollAnchorAfterPrepend(
    viewport,
    () => { viewport.scrollHeight = 1_450; },
    callback => { scheduled = callback; },
  );
  expect(viewport.scrollTop).toBe(320);
  (scheduled as (() => void) | null)?.();
  expect(viewport.scrollTop).toBe(770);
});

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

  test('scroll-to-bottom button appears when showScrollDown=true in triage mode', async () => {
    const msg = makeMsg({ id: 'n1', message_type: 'NOTE', content: 'test message' });
    await renderView(baseProps({
      presentation: 'triage',
      messages: [msg],
      otherHighSignal: [msg],
      showScrollDown: true,
    }));
    // The triage rows render message content through MarkdownContent, which is
    // code-split via React.lazy (PR #13146). On first paint that lazy chunk
    // suspends, so the surrounding update — including the scroll-to-bottom
    // control that sits beside the virtualized list — has not committed yet.
    // Poll until the render settles rather than asserting mid-suspense.
    await vi.waitFor(() => {
      expect(host?.querySelector('button[aria-label="Scroll to latest message"]')).toBeTruthy();
    });
  });

  test('conversation virtualizes a 1,000-message history to a bounded DOM', async () => {
    const messages = Array.from({ length: 1_000 }, (_, index) => makeMsg({
      id: `conversation-${index + 1}`,
      sequence: index + 1,
      from_type: index % 2 ? 'strategic' : 'user',
      content: `Conversation message ${index + 1}`,
      surface: 'conversation',
    }));
    await renderView(baseProps({
      presentation: 'conversation',
      messages,
    }));
    await vi.waitFor(() => {
      expect(host?.querySelector('[data-testid="strategic-conversation-column"]')).toBeTruthy();
      const renderedBubbles = host?.querySelectorAll('[data-testid="message-bubble"]').length ?? 0;
      expect(renderedBubbles).toBeGreaterThan(0);
      expect(renderedBubbles).toBeLessThan(40);
    });
  });

  test('imperative conversation navigation renders an offscreen target before resolving', async () => {
    const messages = Array.from({ length: 200 }, (_, index) => makeMsg({
      id: `target-history-${index + 1}`,
      sequence: index + 1,
      from_type: index % 2 ? 'strategic' : 'user',
      content: `Target history ${index + 1}`,
      surface: 'conversation',
    }));
    const conversationListRef = createRef<ConversationListImperativeApi>();
    await renderView(baseProps({ presentation: 'conversation', messages, conversationListRef }));
    expect(host?.querySelector('[data-message-id="target-history-150"]')).toBeNull();
    let rendered = false;
    let navigation!: Promise<boolean>;
    act(() => { navigation = conversationListRef.current!.scrollToMessage('target-history-150'); });
    rendered = await navigation;
    expect(rendered).toBe(true);
    expect(host?.querySelector('[data-message-id="target-history-150"]')).not.toBeNull();
  });

  test.each(['triage', 'conversation'] as const)('%s virtual rows retain listitem ARIA position metadata', async presentation => {
    const messages = Array.from({ length: 4 }, (_, index) => makeMsg({
      id: `aria-${presentation}-${index + 1}`,
      sequence: index + 1,
      from_type: presentation === 'conversation' ? 'strategic' : 'system',
      surface: presentation === 'conversation' ? 'conversation' : 'activity',
    }));
    await renderView(baseProps({
      presentation,
      messages,
      otherHighSignal: presentation === 'triage' ? messages : [],
    }));
    await vi.waitFor(() => {
      const item = host?.querySelector('[role="listitem"]');
      expect(item).toBeTruthy();
      expect(item?.getAttribute('aria-posinset')).toBe('1');
      expect(Number(item?.getAttribute('aria-setsize'))).toBeGreaterThanOrEqual(4);
    });
  });

  test('mute_active_banner renders in collapsed routine without breaking layout', async () => {
    const msg = makeMsg({ id: 'r1', message_type: 'NOTE', content: 'routine message' });
    await expectNoOverflowAt(375, async () => {
      await renderView(baseProps({
        presentation: 'triage',
        messages: [msg],
        routineOpen: [msg],
        muteActive: true,
        routineExpanded: false,
      }));
    });
  });

  test('scrollToBottom scrolls to the last row via list API', async () => {
    const messages = Array.from({ length: 10 }, (_, index) => makeMsg({
      id: `scroll-test-${index + 1}`,
      sequence: index + 1,
      from_type: index % 2 ? 'strategic' : 'user',
      content: `Scroll message ${index + 1}`,
      surface: 'conversation',
    }));
    const conversationListRef = createRef<ConversationListImperativeApi>();
    await renderView(baseProps({ presentation: 'conversation', messages, conversationListRef }));
    await vi.waitFor(() => {
      expect(conversationListRef.current?.scrollToBottom).toBeDefined();
    });
    // Calling scrollToBottom should not throw
    expect(() => conversationListRef.current?.scrollToBottom()).not.toThrow();
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

  test('mute_active_banner appears when muteActive=true and routineExpanded=false (collapsed routine)', () => {
    const msg = makeMsg({ id: 'r1' });
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      routineOpen: [msg],
      muteActive: true,
      routineExpanded: false,
    }));
    const bannerItem = items.find(item => item.kind === 'mute_active_banner');
    expect(bannerItem).toBeDefined();
    expect(bannerItem?.kind).toBe('mute_active_banner');
  });

  test('mute_active_banner appears when muteActive=true and routineExpanded=true (expanded routine)', () => {
    const msg = makeMsg({ id: 'r1' });
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      routineOpen: [msg],
      muteActive: true,
      routineExpanded: true,
    }));
    const bannerItem = items.find(item => item.kind === 'mute_active_banner');
    expect(bannerItem).toBeDefined();
    expect(bannerItem?.kind).toBe('mute_active_banner');
  });

  test('mute_active_banner does not appear when muteActive=false', () => {
    const msg = makeMsg({ id: 'r1' });
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      routineOpen: [msg],
      muteActive: false,
      routineExpanded: false,
    }));
    const bannerItem = items.find(item => item.kind === 'mute_active_banner');
    expect(bannerItem).toBeUndefined();
  });

  test('mute_active_banner positioned after routine_head in collapsed state', () => {
    const msg = makeMsg({ id: 'r1' });
    const { items } = buildTriageItems(makeParams({
      messages: [msg],
      routineOpen: [msg],
      muteActive: true,
      routineExpanded: false,
    }));
    const routineHeadIdx = items.findIndex(item => item.kind === 'routine_head');
    const bannerIdx = items.findIndex(item => item.kind === 'mute_active_banner');
    expect(routineHeadIdx).toBeGreaterThanOrEqual(0);
    expect(bannerIdx).toBeGreaterThan(routineHeadIdx);
  });
});
