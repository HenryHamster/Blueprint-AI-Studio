import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { ProjectDashboard } from '../components/ProjectDashboard';
import { AlertsBanner } from '../components/AlertsBanner';
import { PipelineView } from '../components/PipelineView';
import { expectNoOverflowAt } from './viewport';
import type { Task } from '../types/task';
import type { Project } from '../types/project';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  const digestPayload = {
    daily_cost_usd: 62.0,
    daily_cost_ceiling_usd: 100,
    window_hours: 24,
    generated_at: '2026-05-06T00:00:00Z',
    completed_count: 2,
    failed_count: 0,
    pending_approval_count: 0,
    research_cycles: 0,
    prs_opened: 1,
    active_count: 1,
    running_count: 1,
    needs_attention: [],
    recent_failures: [],
    failure_groups: [],
    recent_completions: [],
    direction_progress: [],
    active_tasks: [],
    fresh_reviewer_effectiveness: null,
  };
  return {
    ...actual,
    API_BASE_URL: 'http://localhost:8000',
    apiFetch: vi.fn(async (url: string) => {
      if (url.includes('/api/admin/digest')) {
        return new Response(JSON.stringify(digestPayload), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }),
    authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  };
});

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
    deploy_monitoring: null,
  },
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: Date.UTC(2026, 4, 7, 0, 0, 0),
  tasks: [],
  pipelineThrottle: null,
};

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [],
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'feat: pipeline composed viewport regression fixture',
    description: '',
    status: 'QUEUED',
    task_type: 'create',
    created_at: '2026-05-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    history: [],
    ...overrides,
  };
}

const tasks: Task[] = [
  makeTask({
    id: 'task-parent',
    title: 'feat: pipeline composed viewport regression fixture',
    status: 'IN_PROGRESS',
    task_type: 'create',
    started_at: '2026-05-01T01:00:00Z',
  }),
  makeTask({
    id: 'task-child-1',
    title: 'review: pipeline layout change review',
    status: 'QUEUED',
    task_type: 'review',
    parent_task_id: 'task-parent',
  }),
  makeTask({
    id: 'task-child-2',
    title: 'fix: pipeline mobile overflow fix iteration',
    status: 'COMPLETED',
    task_type: 'fix',
    parent_task_id: 'task-parent',
  }),
  makeTask({
    id: 'task-solo',
    title: 'research: standalone pipeline viewport investigation',
    status: 'EVALUATING',
    task_type: 'research',
  }),
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
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.className = 'flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <>
            <div data-testid="header-bar-stub" />
            <OrchestratorHealthBar {...healthBarProps} />
            <ProjectDashboard
              projects={projects}
              tasks={tasks}
              selectedProjectId="p1"
              onSelectProject={() => {}}
            />
            <AlertsBanner alerts={[]} onDismiss={() => {}} />
            <div data-testid="search-filter-stub" className="flex items-center gap-2 shrink-0 mb-1 flex-wrap" />
            <main className="flex-1 overflow-hidden flex flex-col">
              <PipelineView tasks={tasks} onViewLogs={() => {}} />
            </main>
          </>,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });

    expect(host!.textContent).toContain('feat: pipeline composed viewport regression fixture');

    localStorage.removeItem('projectDashboardCollapsed');
  });
});
