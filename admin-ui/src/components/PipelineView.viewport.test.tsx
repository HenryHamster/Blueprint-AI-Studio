import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PipelineView } from './PipelineView';
import type { Task } from './TaskCard';
import { expectNoOverflowAt } from '../test/viewport';

// PipelineView imports '../utils/time' internally; no API calls needed for this
// test, so we don't mock apiFetch.

const sampleTasks: Task[] = [
  // Parent task
  {
    id: 'task-parent-1',
    title: 'research: analyze mobile rendering performance bottlenecks',
    description: '',
    status: 'COMPLETED',
    task_type: 'research',
    created_by: 'test-user',
    requires_approval: false,
    created_at: '2026-05-01T10:00:00Z',
    history: [
      { timestamp: '2026-05-01T12:00:00Z', action: 'completed', details: {} },
    ],
  },
  // Child task 1 with depends_on
  {
    id: 'task-child-1',
    title: 'create: implement responsive grid layout for DashboardHome',
    description: '',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'test-user',
    requires_approval: false,
    parent_task_id: 'task-parent-1',
    depends_on: ['task-parent-1'],
    created_at: '2026-05-01T12:30:00Z',
    pr_url: 'https://github.com/example/repo/pull/42',
  },
  // Child task 2 with depends_on (different parent for testing cross-tree deps)
  {
    id: 'task-child-2',
    title: 'fix: resolve viewport overflow in PipelineView component',
    description: '',
    status: 'QUEUED',
    task_type: 'fix',
    created_by: 'test-user',
    requires_approval: false,
    parent_task_id: 'task-parent-1',
    depends_on: ['task-child-1'],
    created_at: '2026-05-01T13:00:00Z',
    payloads: [{ pr_number: 42 }],
  },
];

describe('PipelineView — viewport regression', () => {
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
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.display = 'flex';
      host.style.flexDirection = 'column';
      host.style.height = '100vh';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PipelineView
            tasks={sampleTasks}
            onViewLogs={() => {}}
          />,
        );
      });
    });
  });
});
