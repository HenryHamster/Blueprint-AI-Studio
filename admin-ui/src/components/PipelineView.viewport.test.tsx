import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PipelineView } from './PipelineView';
import type { Task } from './TaskCard';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// expectMinTouchTargetAt is intentionally omitted: PipelineView is a dense
// list UI where every button is deliberately compact (filter chips, expand/collapse
// chevrons, PR/orphan badges). Small targets are documented in PipelineView.tsx
// with inline disable comments for the viewport/min-touch-target lint rule.

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

  test('task titles and status text are not silently clipped at 375px', async () => {
    const longTitleTasks: Task[] = [
      {
        id: 'task-long-1',
        title: 'research: analyze mobile rendering performance bottlenecks across multiple pipeline stages',
        description: '',
        status: 'IN_PROGRESS',
        task_type: 'research',
        created_by: 'test-user',
        requires_approval: false,
        created_at: '2026-05-01T10:00:00Z',
        latest_status_message: 'Investigating viewport overflow patterns in PipelineView component',
      },
      {
        id: 'task-long-2',
        title: 'fix: resolve horizontal overflow in pipeline regression dispatcher component',
        description: '',
        status: 'QUEUED',
        task_type: 'fix',
        created_by: 'test-user',
        requires_approval: false,
        parent_task_id: 'task-long-1',
        created_at: '2026-05-01T11:00:00Z',
      },
    ];

    await expectNoTextTruncationAt(375, async () => {
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
            tasks={longTitleTasks}
            onViewLogs={() => {}}
          />,
        );
      });
    });
  });
});
