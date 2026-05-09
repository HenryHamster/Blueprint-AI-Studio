import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { BulkActionBar } from './BulkActionBar';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const createSampleTask = (id: string, status: string, title: string): Task => ({
  id,
  title,
  description: '',
  status: status as 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'COMPLETED' | 'FAILED' | 'CANCELLED',
  created_by: 'user-1',
  requires_approval: status === 'EVALUATING',
  created_at: '2026-05-06T00:00:00Z',
});

describe('BulkActionBar — viewport regression', () => {
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

  test('renders with all action buttons without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      // Create sample tasks with different statuses
      const approvableTasks = [
        createSampleTask('task-1', 'EVALUATING', 'feat: add mobile-responsive layout'),
        createSampleTask('task-2', 'EVALUATING', 'fix: resolve authentication timeout issue'),
      ];

      const cancellableTasks = [
        createSampleTask('task-3', 'IN_PROGRESS', 'refactor: simplify component state management'),
      ];

      const failedTasks = [
        createSampleTask('task-4', 'FAILED', 'test: add comprehensive integration tests'),
        createSampleTask('task-5', 'FAILED', 'docs: update API documentation with examples'),
      ];

      const terminalTasks = [
        createSampleTask('task-6', 'COMPLETED', 'chore: update dependencies to latest versions'),
        createSampleTask('task-7', 'CANCELLED', 'experiment: evaluate new caching strategy'),
      ];

      const allTasks = [...approvableTasks, ...cancellableTasks, ...failedTasks, ...terminalTasks];

      await act(async () => {
        r.render(
          <BulkActionBar
            selectedCount={allTasks.length}
            approvableTasks={approvableTasks}
            cancellableTasks={cancellableTasks}
            failedTasks={failedTasks}
            terminalTasks={terminalTasks}
            setConfirmDialog={() => {}}
            handleBulkAction={() => {}}
            handleBulkCancel={() => {}}
            handleBulkArchiveColumn={() => {}}
            toggleSelectionMode={() => {}}
          />,
        );
      });
    });
  });

  test('renders with no action buttons available without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <BulkActionBar
            selectedCount={1}
            approvableTasks={[]}
            cancellableTasks={[]}
            failedTasks={[]}
            terminalTasks={[]}
            setConfirmDialog={() => {}}
            handleBulkAction={() => {}}
            handleBulkCancel={() => {}}
            handleBulkArchiveColumn={() => {}}
            toggleSelectionMode={() => {}}
          />,
        );
      });
    });
  });

  test('selected-count label and button labels do not silently clip at 375px', async () => {
    const approvableTasks = [
      createSampleTask('task-1', 'EVALUATING', 'feat(auth): implement token-refresh flow with sliding expiry and cross-tab sync for long-running operator sessions'),
      createSampleTask('task-2', 'EVALUATING', 'fix(backend): handle edge case where pipeline_regression_dispatcher emits duplicate escalation_reason labels on retry'),
    ];
    const cancellableTasks = [
      createSampleTask('task-3', 'IN_PROGRESS', 'refactor: simplify component state management across kanban board and task detail drawer to reduce prop-drilling depth'),
    ];
    const failedTasks = [
      createSampleTask('task-4', 'FAILED', 'test: add comprehensive integration tests for regression dispatcher retry logic and dedup guard path'),
      createSampleTask('task-5', 'FAILED', 'docs: update API documentation with examples for all admin endpoints including pagination and filtering'),
    ];
    const terminalTasks: Task[] = [];

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <BulkActionBar
            selectedCount={approvableTasks.length + cancellableTasks.length + failedTasks.length}
            approvableTasks={approvableTasks}
            cancellableTasks={cancellableTasks}
            failedTasks={failedTasks}
            terminalTasks={terminalTasks}
            setConfirmDialog={() => {}}
            handleBulkAction={() => {}}
            handleBulkCancel={() => {}}
            handleBulkArchiveColumn={() => {}}
            toggleSelectionMode={() => {}}
          />,
        );
      });
    });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    const approvableTasks = [
      createSampleTask('task-1', 'EVALUATING', 'feat: add mobile-responsive layout'),
      createSampleTask('task-2', 'EVALUATING', 'fix: resolve authentication timeout issue'),
    ];
    const cancellableTasks = [
      createSampleTask('task-3', 'IN_PROGRESS', 'refactor: simplify component state management'),
    ];
    const failedTasks = [
      createSampleTask('task-4', 'FAILED', 'test: add comprehensive integration tests'),
    ];
    const terminalTasks = [
      createSampleTask('task-5', 'COMPLETED', 'chore: update dependencies to latest versions'),
    ];

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <BulkActionBar
            selectedCount={approvableTasks.length + cancellableTasks.length + failedTasks.length + terminalTasks.length}
            approvableTasks={approvableTasks}
            cancellableTasks={cancellableTasks}
            failedTasks={failedTasks}
            terminalTasks={terminalTasks}
            setConfirmDialog={() => {}}
            handleBulkAction={() => {}}
            handleBulkCancel={() => {}}
            handleBulkArchiveColumn={() => {}}
            toggleSelectionMode={() => {}}
          />,
        );
      });
    });
  });
});
