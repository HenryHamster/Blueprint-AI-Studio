import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ArchiveView from './ArchiveView';
import { expectNoOverflowAt } from '../test/viewport';
import type { Task } from './TaskCard';

describe('ArchiveView — viewport regression', () => {
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
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    const mockTasks: Task[] = [
      {
        id: 'task-1',
        title: 'feat: implement mobile-responsive layout for dashboard',
        description: 'Add Tailwind sm: breakpoints to main grid',
        status: 'COMPLETED',
        task_type: 'create',
        created_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
        pr_url: 'https://github.com/example/repo/pull/100',
        duration_seconds: 3600,
      },
      {
        id: 'task-2',
        title: 'fix: sidebar overflow on mobile viewports',
        description: 'Constrain sidebar width to 95vw',
        status: 'COMPLETED',
        task_type: 'fix',
        created_at: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
        pr_url: null,
        duration_seconds: 1800,
      },
      {
        id: 'task-3',
        title: 'refactor: extract form components to reduce bundle size',
        description: null,
        status: 'FAILED',
        task_type: 'refactor',
        created_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
        pr_url: 'https://github.com/example/repo/pull/98',
        duration_seconds: 5400,
      },
      {
        id: 'task-4',
        title: 'test: add viewport regression tests for core pages',
        description: 'Create .viewport.test.tsx files',
        status: 'CANCELLED',
        task_type: 'create',
        created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
        pr_url: null,
        duration_seconds: 2700,
      },
      {
        id: 'task-5',
        title: 'docs: update README with new viewport testing patterns',
        description: 'Document expectNoOverflowAt usage',
        status: 'COMPLETED',
        task_type: 'create',
        created_at: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString(),
        pr_url: 'https://github.com/example/repo/pull/95',
        duration_seconds: 900,
      },
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ArchiveView
            archiveTasks={mockTasks}
            archiveLoading={false}
            archivePage={1}
            archivePageSize={25}
            archiveStatusFilter="all"
            archiveSearchQuery=""
            archiveDateRange="all"
            archiveTotalTasks={5}
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
          />,
        );
      });
    });
  });
});
