import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ArchiveView from './ArchiveView';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import type { Task } from './TaskCard';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-archive-1',
    title: 'feat: implement mobile-responsive layout for the main operator dashboard view',
    description: 'Add responsive breakpoints and keep archive controls reachable.',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    pr_url: 'https://github.com/example/repo/pull/100',
    requires_approval: false,
    created_by: 'tester',
    ...overrides,
  };
}

const archiveTasks: Task[] = [
  makeTask(),
  makeTask({
    id: 'task-archive-2',
    title: 'fix: sidebar overflow on mobile viewports causes horizontal scroll',
    description: 'Constrain archived task actions without hiding retry and unarchive.',
    status: 'FAILED',
    task_type: 'fix',
    created_at: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    pr_url: undefined,
  }),
  makeTask({
    id: 'task-archive-3',
    title: 'test: add viewport regression tests for archive controls',
    description: '',
    status: 'CANCELLED',
    task_type: 'create',
    created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
    pr_url: undefined,
  }),
];

describe('ArchiveView — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  function cleanupRenderRoot() {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    host?.remove();
    host = null;
  }

  afterEach(() => {
    cleanupRenderRoot();
  });

  async function renderArchive() {
    cleanupRenderRoot();
    host = document.createElement('div');
    host.style.height = '720px';
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ArchiveView
          archiveTasks={archiveTasks}
          archiveLoading={false}
          archivePage={1}
          archivePageSize={25}
          archiveStatusFilter="all"
          archiveSearchQuery=""
          archiveDateRange="all"
          archiveTotalTasks={75}
          setShowArchive={() => {}}
          setArchivePage={() => {}}
          setArchivePageSize={() => {}}
          setArchiveStatusFilter={() => {}}
          setArchiveSearchQuery={() => {}}
          setArchiveDateRange={() => {}}
          handleViewLogs={() => {}}
          onRetryTask={() => {}}
          onUnarchiveTask={() => {}}
          archiveSortOrder="desc"
          setArchiveSortOrder={() => {}}
          onPurgeArchive={async () => {}}
        />,
      );
    });
  }

  async function renderArchiveWithPurgeDialog() {
    await renderArchive();
    const clearArchive = host?.querySelector<HTMLButtonElement>('button[aria-controls="archive-purge-menu"]');
    if (!clearArchive) throw new Error('Expected clear archive button');
    await act(async () => {
      clearArchive.click();
    });
    const purgeOption = Array.from(host?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
      .find(button => button.textContent?.includes('Older than 90 days'));
    if (!purgeOption) throw new Error('Expected purge option');
    await act(async () => {
      purgeOption.click();
    });
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderArchive, { height: 720 });
  });

  test('visible labels and pagination are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, renderArchive, { height: 720 });
  });

  test('interactive archive controls meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, renderArchive, {
      height: 720,
      selector: 'button,a,select,input',
    });
  });

  test('purge menu and confirm dialog fit mobile viewport', async () => {
    await expectNoOverflowAt(375, renderArchiveWithPurgeDialog, { height: 720 });
    await expectMinTouchTargetAt(375, renderArchiveWithPurgeDialog, {
      height: 720,
      selector: 'button,a,select,input',
    });
  });

  test('desktop keeps task list wide and actions panel in the right rail', async () => {
    await expectNoOverflowAt(1280, renderArchive, { height: 800 });

    const taskList = host?.querySelector<HTMLElement>('[data-testid="archive-task-list"]');
    const actionsPanel = host?.querySelector<HTMLElement>('[data-testid="archive-actions-panel"]');
    if (!taskList || !actionsPanel) {
      throw new Error('Expected Archive task list and actions panel');
    }

    const taskRect = taskList.getBoundingClientRect();
    const actionsRect = actionsPanel.getBoundingClientRect();
    expect(taskRect.width).toBeGreaterThan(actionsRect.width);
    expect(actionsRect.left).toBeGreaterThanOrEqual(taskRect.right - 1);
  });
});
