import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TimelineView } from './TimelineView';
import { expectNoOverflowAt } from '../test/viewport';
import type { Task } from '../types/task';

vi.mock('lucide-react', () => ({
  ChevronDown: () => null,
  ChevronRight: () => null,
}));

const BASE_TIME = new Date('2026-05-06T10:00:00Z').getTime();

function makeTask(overrides: Partial<Task> & { id: string }): Task {
  return {
    id: overrides.id,
    title: overrides.title ?? `Task ${overrides.id} with a long descriptive title that exercises truncation`,
    description: '',
    status: overrides.status ?? 'IN_PROGRESS',
    task_type: overrides.task_type ?? 'create',
    created_by: 'agent',
    requires_approval: false,
    created_at: overrides.created_at ?? new Date(BASE_TIME - 2 * 3600 * 1000).toISOString(),
    started_at: overrides.started_at ?? new Date(BASE_TIME - 1.5 * 3600 * 1000).toISOString(),
    last_heartbeat: overrides.last_heartbeat ?? new Date(BASE_TIME - 60 * 1000).toISOString(),
    latest_status_message: overrides.latest_status_message,
    parent_task_id: overrides.parent_task_id,
    status_log: overrides.status_log,
    ...overrides,
  };
}

const TASKS: Task[] = [
  makeTask({ id: 't1', title: 'feat: add viewport coverage for timeline panel', status: 'COMPLETED', status_log: [{ t: new Date(BASE_TIME - 30 * 60 * 1000).toISOString(), msg: 'done' }] }),
  makeTask({ id: 't2', title: 'fix: wrap long labels on mobile narrow viewport', status: 'FAILED', status_log: [{ t: new Date(BASE_TIME - 20 * 60 * 1000).toISOString(), msg: 'failed' }] }),
  makeTask({ id: 't3', title: 'feat: expose signal_first_crossed_at in regression cases API', status: 'IN_PROGRESS', last_heartbeat: new Date(BASE_TIME - 5 * 1000).toISOString() }),
  makeTask({ id: 't4', title: 'feat: scope-drift checks for off-scope file modifications', status: 'EVALUATING' }),
  makeTask({ id: 't4a', parent_task_id: 't4', title: 'sub: verify scope boundaries in standard handler', status: 'COMPLETED', created_at: new Date(BASE_TIME - 1.8 * 3600 * 1000).toISOString(), started_at: new Date(BASE_TIME - 1.6 * 3600 * 1000).toISOString(), status_log: [{ t: new Date(BASE_TIME - 40 * 60 * 1000).toISOString(), msg: 'done' }] }),
  makeTask({ id: 't4b', parent_task_id: 't4', title: 'sub: update test assertions for new invariant', status: 'IN_PROGRESS', created_at: new Date(BASE_TIME - 1.5 * 3600 * 1000).toISOString(), started_at: new Date(BASE_TIME - 1.3 * 3600 * 1000).toISOString() }),
  makeTask({ id: 't5', title: 'feat: surface edges-vs-correctness gap on Edges card', status: 'COMPLETED', created_at: new Date(BASE_TIME - 3 * 3600 * 1000).toISOString(), started_at: new Date(BASE_TIME - 2.8 * 3600 * 1000).toISOString(), status_log: [{ t: new Date(BASE_TIME - 2 * 3600 * 1000).toISOString(), msg: 'done' }] }),
  makeTask({ id: 't6', title: 'research: autonomous loop diagnoses pipeline regressions', status: 'EVALUATING', task_type: 'research', latest_status_message: 'Evaluating findings across 12 regression cases from the last 7 days' }),
  makeTask({ id: 't7', title: 'fix: PR #3997 blocked by failing CI checks after rebase conflict', status: 'IN_PROGRESS', task_type: 'fix' }),
  makeTask({ id: 't7a', parent_task_id: 't7', title: 'sub: resolve rebase conflict in alembic migration', status: 'COMPLETED', created_at: new Date(BASE_TIME - 1.2 * 3600 * 1000).toISOString(), started_at: new Date(BASE_TIME - 1.1 * 3600 * 1000).toISOString(), status_log: [{ t: new Date(BASE_TIME - 50 * 60 * 1000).toISOString(), msg: 'done' }] }),
  makeTask({ id: 't8', title: 'create: collapse routine ops in messages panel', status: 'QUEUED', started_at: undefined }),
];

describe('TimelineView — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root.unmount()); root = null; host?.remove(); host = null; });

  test('renders without horizontal overflow at 375px with dense events', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <TimelineView
            tasks={TASKS}
            hasActiveFilters={false}
            onViewLogs={() => {}}
          />
        );
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('renders without horizontal overflow at 375px with filters active', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <TimelineView
            tasks={TASKS.slice(0, 3)}
            hasActiveFilters={true}
            onViewLogs={() => {}}
          />
        );
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
