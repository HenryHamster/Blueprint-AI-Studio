import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesListView, type MessagesListViewProps } from './MessagesListView';
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
    blockersRefEl: createRef<HTMLDivElement>(),
    questionsRefEl: createRef<HTMLDivElement>(),
    routineRefEl: createRef<HTMLDivElement>(),
    dividerDismissedRef: { current: false },
    messages: [],
    loading: false,
    dividerTime: null,
    totalNewCount: 0,
    newMessageCounts: { blockers: 0, questions: 0, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0 },
    highSignalOpen: [],
    routineOpen: [],
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
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.width = '375px';
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
        highSignalOpen: [high, other],
        pinnedBlockers: [high],
        otherHighSignal: [other],
      }));
    });
  });

  test('populated list has no silent text truncation at 375px', async () => {
    const other = makeMsg({ id: 'n1', message_type: 'NOTE', content: stressStrings.directionText });
    await expectNoTextTruncationAt(375, async () => {
      await renderView(baseProps({ messages: [other], highSignalOpen: [other], otherHighSignal: [other] }));
    });
  });

  test('action buttons meet minimum touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await renderView(baseProps({ showScrollDown: true, hiddenResolvedCount: 3 }));
    });
  });
});
