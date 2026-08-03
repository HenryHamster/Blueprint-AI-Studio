import { afterEach, describe, expect, test } from 'vitest';
import { page } from 'vitest/browser';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PipelineWorkspace, type PipelineWorkspaceProps } from './PipelineWorkspace';
import type { Task } from './TaskCard';
import type { TrackLogEntry } from '../types/task';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { COLUMNS } from '../appWorkbenchConstants';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-pipeline-1',
    project_id: 'proj-1',
    title: 'feat: keep pipeline monitoring accessible on compact workbench viewports',
    description: '',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'tester',
    requires_approval: false,
    created_at: '2026-06-30T10:00:00Z',
    started_at: '2026-06-30T10:02:00Z',
    latest_status_message: 'Streaming runner output while the operator reviews pipeline state.',
    ...overrides,
  };
}

const trackedTask = makeTask();
const queuedTask = makeTask({
  id: 'task-pipeline-2',
  title: 'fix: polish sideboard spacing without changing the task context rail',
  status: 'QUEUED',
  started_at: undefined,
});
const blockedTask = makeTask({
  id: 'task-pipeline-3',
  title: 'review: verify mobile viewport coverage for pipeline sideboard',
  status: 'BLOCKED',
  started_at: undefined,
});

const trackedLogs: TrackLogEntry[] = [
  { id: 1, t: '2026-06-30T12:00:01Z', type: 'system', msg: 'stream attached' },
  { id: 2, t: '2026-06-30T12:00:02Z', type: 'log', msg: 'agent.run: checking sideboard composition at mobile width' },
  { id: 3, t: '2026-06-30T12:00:03Z', type: 'status', msg: 'IN_PROGRESS' },
];

function workspaceProps(): PipelineWorkspaceProps {
  const tasks = [trackedTask, queuedTask, blockedTask];
  return {
    canOperateProject: true,
    surface: {
      pipelineSection: 'flow',
      onPipelineSectionChange: () => {},
      showArchive: false,
      setShowArchive: () => {},
      viewMode: 'pipeline',
      setViewMode: () => {},
    },
    chrome: {
      actions: null,
      filterStrip: (
        <div className="flex min-w-0 items-center gap-2 overflow-x-auto border-b border-neutral-800 bg-[#070b11] px-3 py-2 text-xs text-neutral-400">
          <span className="shrink-0">Filters</span>
          <span className="shrink-0 rounded border border-neutral-800 px-2 py-1">active</span>
          <span className="shrink-0 rounded border border-neutral-800 px-2 py-1">blocked</span>
        </div>
      ),
      pagination: (
        <div className="shrink-0 border-t border-neutral-800 bg-[#05070a] px-3 py-1.5 text-xs text-neutral-500">
          page 1 of 1
        </div>
      ),
    },
    archive: {} as PipelineWorkspaceProps['archive'],
    flow: {
      tasks,
      hasActiveFilters: false,
      onViewLogs: () => {},
      onNavigateToParent: () => {},
      onArchiveGroup: () => {},
      onUnarchiveTask: () => {},
    },
    rail: {
      currentTask: trackedTask,
      attentionTasks: [blockedTask],
      localFallbackTasks: [queuedTask],
      trackedTaskIds: new Set([trackedTask.id]),
      taskContextSessionProps: { sessionRefs: [], savingSessionRefs: false, onUpdateSessionRefs: () => {} },
      onOpenTask: () => {},
      onOpenPipeline: () => {},
      onTrackTask: () => {},
    },
    board: {
      tasks,
      columns: COLUMNS,
      loading: false,
      maxRetries: 3,
      actionLoadingMap: {},
      compact: false,
      sortBy: 'newest',
      selectionMode: false,
      selectedTaskIds: new Set<string>(),
      onToggleTaskSelection: () => {},
      onCtrlClick: () => {},
      onBulkToggle: () => {},
      onShiftSelect: () => {},
      onArchiveColumn: () => {},
      onApprove: () => {},
      onRequestChanges: () => {},
      onCancel: () => {},
      onRetry: () => {},
      onEditPrompt: () => {},
      onDismiss: () => {},
      onArchive: () => {},
      onViewLogs: () => {},
      onTrack: () => {},
      trackedTaskIds: new Set([trackedTask.id]),
      onDuplicate: () => {},
      hasActiveFilters: false,
      searchQuery: '',
      onDirectionClick: () => {},
    },
    metrics: {
      activeTasks: [trackedTask],
      queuedTasks: [queuedTask],
      blockedTasks: [blockedTask],
      failedTasks: [],
      terminalTasks: [],
    },
    status: {
      selectedProjectId: 'proj-1',
      selectedProjectLabel: 'blueprint/admin-ui',
      displayedRows: tasks.length,
      loadedRows: tasks.length,
      serverTotalRows: tasks.length,
      lastFetchTime: Date.parse('2026-06-30T12:00:00Z'),
      apiHealthy: true,
      wsConnected: true,
    },
    stream: {
      trackedTaskIds: new Set([trackedTask.id]),
      trackedLogs: new Map([[trackedTask.id, trackedLogs]]),
      tasks,
      onUntrack: () => {},
      onUntrackAll: () => {},
    },
  };
}

describe('PipelineWorkspace viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    host?.remove();
    host = null;
  });

  async function renderWorkspace(section: PipelineWorkspaceProps['surface']['pipelineSection'] = 'flow') {
    host = document.createElement('div');
    host.style.height = '720px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    const props = workspaceProps();
    props.surface.pipelineSection = section;
    await act(async () => {
      r.render(<PipelineWorkspace {...props} />);
    });
    // KanbanBoard / PipelineView are React.lazy behind <Suspense fallback={null}>,
    // so the main region mounts empty until the chunk resolves. Wait for real
    // content before returning — otherwise the overflow/content assertions run
    // against an empty <main> and the outcome depends on module-cache timing
    // (first board render historically raced and failed nondeterministically).
    const mainTestId = section === 'board' ? 'pipeline-board-main' : 'pipeline-flow-main';
    const deadline = Date.now() + 5000;
    for (;;) {
      const main = host.querySelector<HTMLElement>(`[data-testid="${mainTestId}"]`);
      if (main && (main.textContent ?? '').trim().length > 0) break;
      if (Date.now() > deadline) {
        throw new Error(`Timed out waiting for lazy ${section} section content to mount`);
      }
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 25));
      });
    }
  }

  async function expectTaskContextReachable(sideboard: HTMLElement, aside: HTMLElement) {
    const taskContext = sideboard.querySelector<HTMLElement>('[data-testid="pipeline-task-context"]');
    if (!taskContext) {
      throw new Error('Expected Pipeline sideboard task context to render');
    }
    expect(taskContext.textContent).toContain('Task context');
    expect(taskContext.textContent).toContain(trackedTask.title);
    await act(async () => {
      taskContext.scrollIntoView({ block: 'nearest' });
    });
    const asideRect = aside.getBoundingClientRect();
    const contextRect = taskContext.getBoundingClientRect();
    expect(contextRect.top).toBeLessThanOrEqual(asideRect.bottom - 44);
    expect(contextRect.bottom).toBeGreaterThanOrEqual(asideRect.top + 44);
  }

  async function expectTaskStreamReachable(sideboard: HTMLElement, aside: HTMLElement) {
    const stream = sideboard.querySelector<HTMLElement>('[data-testid="task-stream-panel"]');
    if (!stream) {
      throw new Error('Expected embedded task stream to render');
    }
    expect(stream.dataset.variant).toBe('embedded');
    expect(stream.textContent).toContain('Task stream');
    expect(stream.textContent).toContain('3 entries');
    await act(async () => {
      stream.scrollIntoView({ block: 'nearest' });
    });
    const asideRect = aside.getBoundingClientRect();
    const streamRect = stream.getBoundingClientRect();
    expect(streamRect.top).toBeLessThanOrEqual(asideRect.bottom - 44);
    expect(streamRect.bottom).toBeGreaterThanOrEqual(asideRect.top + 44);
  }

  test('mobile flow exposes monitoring sideboard without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderWorkspace, { height: 720 });

    if (!host) throw new Error('Expected viewport test host to be mounted');
    const sideboard = host.querySelector<HTMLElement>('[data-testid="pipeline-sideboard"]');
    const aside = sideboard?.closest('aside') as HTMLElement | null;
    const main = host.querySelector<HTMLElement>('[data-testid="pipeline-flow-main"]');
    if (!sideboard || !aside || !main) {
      throw new Error('Expected Pipeline flow sideboard and main region to render');
    }
    const nowPanel = sideboard.querySelector<HTMLElement>('[data-testid="pipeline-now-panel"]');
    if (!nowPanel) {
      throw new Error('Expected Pipeline Now panel to render');
    }
    expect(nowPanel.textContent).toContain('Now');
    expect(nowPanel.textContent).toContain('tracked');
    const nowButtons = Array.from(nowPanel.querySelectorAll<HTMLButtonElement>('button[aria-label^="Open "], button[aria-label^="Track "]'));
    if (nowButtons.length === 0) {
      throw new Error('Expected Pipeline Now action buttons to render');
    }
    for (const button of nowButtons) {
      const rect = button.getBoundingClientRect();
      expect(rect.width).toBeGreaterThanOrEqual(44);
      expect(rect.height).toBeGreaterThanOrEqual(44);
    }
    expect(Boolean(aside.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(aside.getBoundingClientRect().right).toBeLessThanOrEqual(376);
    expect(aside.getBoundingClientRect().bottom).toBeLessThanOrEqual(main.getBoundingClientRect().top + 1);
    const detailsButton = host.querySelector<HTMLButtonElement>('button[aria-controls="pipeline-status-details"]');
    const detailsButtonRect = detailsButton?.getBoundingClientRect();
    expect(detailsButtonRect?.width).toBeGreaterThanOrEqual(44);
    expect(detailsButtonRect?.height).toBeGreaterThanOrEqual(44);

    await expectTaskContextReachable(sideboard, aside);
    await expectTaskStreamReachable(sideboard, aside);
  });

  test('mobile flow has no silent text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, renderWorkspace, {
      height: 720,
      selector: '[data-testid="pipeline-now-panel"] *, [data-testid="pipeline-health-panel"] *',
    });
  });

  test('mobile flow controls meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, renderWorkspace, {
      height: 720,
      selector: '[data-testid="pipeline-now-panel"] button, button[aria-controls="pipeline-status-details"]',
    });
  });

  test('mobile board keeps monitoring sideboard reachable above the board at 375px', async () => {
    await expectNoOverflowAt(375, () => renderWorkspace('board'), { height: 720 });

    if (!host) throw new Error('Expected viewport test host to be mounted');
    const sideboard = host.querySelector<HTMLElement>('[data-testid="pipeline-sideboard"]');
    const aside = sideboard?.closest('aside') as HTMLElement | null;
    const main = host.querySelector<HTMLElement>('[data-testid="pipeline-board-main"]');
    if (!sideboard || !aside || !main) {
      throw new Error('Expected Pipeline board sideboard and main region to render');
    }
    expect(sideboard.textContent).toContain('Now');
    expect(sideboard.textContent).toContain('Task health');
    expect(sideboard.textContent).toContain('Task context');
    expect(sideboard.textContent).toContain(trackedTask.title);
    expect(Boolean(aside.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(aside.getBoundingClientRect().right).toBeLessThanOrEqual(376);
    expect(aside.getBoundingClientRect().bottom).toBeLessThanOrEqual(main.getBoundingClientRect().top + 1);
    expect(main.textContent).toContain('In Progress');
    expect(main.textContent).toContain(trackedTask.title);
    const nowButtons = Array.from(sideboard.querySelectorAll<HTMLButtonElement>('[data-testid="pipeline-now-panel"] button'));
    expect(nowButtons.length).toBeGreaterThan(0);
    for (const button of nowButtons) {
      const rect = button.getBoundingClientRect();
      expect(rect.width).toBeGreaterThanOrEqual(44);
      expect(rect.height).toBeGreaterThanOrEqual(44);
    }

    await expectTaskContextReachable(sideboard, aside);
    await expectTaskStreamReachable(sideboard, aside);
  });

  test('desktop flow keeps sideboard in the right column with embedded monitoring', async () => {
    await page.viewport(1280, 800);
    await renderWorkspace();

    if (!host) throw new Error('Expected viewport test host to be mounted');
    const sideboard = host.querySelector<HTMLElement>('[data-testid="pipeline-sideboard"]');
    const aside = sideboard?.closest('aside') as HTMLElement | null;
    const main = host.querySelector<HTMLElement>('[data-testid="pipeline-flow-main"]');
    const stream = sideboard?.querySelector<HTMLElement>('[data-testid="task-stream-panel"]');
    if (!sideboard || !aside || !main) {
      throw new Error('Expected Pipeline desktop sideboard and main region to render');
    }
    expect(stream?.dataset.variant).toBe('embedded');
    expect(sideboard.textContent).toContain('Now');
    expect(sideboard.textContent).toContain('Task health');
    expect(sideboard.textContent).toContain('Task context');
    expect(aside.getBoundingClientRect().left).toBeGreaterThanOrEqual(main.getBoundingClientRect().right - 1);
  });

  test('desktop board keeps monitoring sideboard and current task context visible', async () => {
    await page.viewport(1280, 800);
    await renderWorkspace('board');

    if (!host) throw new Error('Expected viewport test host to be mounted');
    const sideboard = host.querySelector<HTMLElement>('[data-testid="pipeline-sideboard"]');
    const aside = sideboard?.closest('aside') as HTMLElement | null;
    const main = host.querySelector<HTMLElement>('[data-testid="pipeline-board-main"]');
    if (!sideboard || !aside || !main) {
      throw new Error('Expected Pipeline board sideboard and main region to render');
    }
    expect(sideboard.textContent).toContain('Now');
    expect(sideboard.textContent).toContain('Task health');
    expect(sideboard.textContent).toContain('Task context');
    expect(sideboard.textContent).toContain(trackedTask.title);
    expect(aside.getBoundingClientRect().left).toBeGreaterThanOrEqual(main.getBoundingClientRect().right - 1);
  });
});
