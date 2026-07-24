import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { PipelineView } from '../components/PipelineView';
import type { Task } from '../components/TaskCard';
import { expectNoOverflowAt } from './viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  authHeaders: vi.fn(async () => ({})),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
}));

const healthBarProps = {
  health: {
    last_cycle_seconds_ago: 90,
    queued_tasks: 3,
    active_tasks: 1,
    pool_info: {},
    deploy_state: {
      status: 'OK',
      rollback_count: 0,
      rollback_reason: null,
      pause_reason: null,
      monitoring_since: null,
      recent_outcomes: [],
    },
    deploy_monitoring: undefined,
  },
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: 1746576000000,
  tasks: [],
  pipelineThrottle: null,
};

const pipelineTasks: Task[] = [
  {
    id: 'task-parent-1',
    title: 'research: analyze mobile rendering pipeline performance',
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
  {
    id: 'task-child-1',
    title: 'create: implement responsive grid layout for operator dashboard',
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
  {
    id: 'task-child-2',
    title: 'fix: resolve viewport overflow in pipeline view component',
    description: '',
    status: 'QUEUED',
    task_type: 'fix',
    created_by: 'test-user',
    requires_approval: false,
    parent_task_id: 'task-parent-1',
    depends_on: ['task-child-1'],
    created_at: '2026-05-01T13:00:00Z',
  },
];

describe('PipelineView composed with shell panels — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('no horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <div className="flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen">
            <OrchestratorHealthBar {...healthBarProps} />
            <div className="h-10 flex items-center border-b border-neutral-800 shrink-0" aria-label="navigation" />
            <PipelineView
              tasks={pipelineTasks}
              onViewLogs={() => {}}
            />
          </div>,
        );
      });
    }, { waitMs: 75 });

    expect(host!.textContent).toContain('research: analyze mobile rendering pipeline performance');
  });
});
