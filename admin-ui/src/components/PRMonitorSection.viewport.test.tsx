import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PRMonitorSection, type OpenPR } from './PRMonitorSection';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// PRMonitorSection imports `isSafeUrl` from TaskCard, which transitively pulls
// in `../api`. Keep the mock minimal but complete enough for transitive
// destructuring to succeed.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  COST_PER_VM_HR: 0.067,
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  authHeaders: vi.fn(async () => ({})),
}));

const tasks: Task[] = [
  {
    id: 'task-1',
    title: 'feat: viewport regression gate for DashboardHome operator widgets at 375px',
    description: '',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_at: '2026-05-07T10:00:00Z',
    created_by: 'USER',
    requires_approval: false,
  },
  {
    id: 'task-2',
    title: 'fix: residual remediation funnel stalled on REMEDIATING cases',
    description: '',
    status: 'EVALUATING',
    task_type: 'fix',
    created_at: '2026-05-07T11:00:00Z',
    created_by: 'AGENT',
    requires_approval: false,
  },
];

const openPRs: OpenPR[] = [
  {
    pr_number: 4521,
    pr_url: 'https://github.com/example/repo/pull/4521',
    pr_title: 'feat: add viewport regression gate for DashboardHome operator widgets at 375px breakpoint',
    pr_branch: 'blueprint/task-1f83300a-very-long-branch-name-for-edge-case-coverage',
    pr_created_at: '2026-05-07T10:30:00Z',
    tasks: [
      {
        task_id: 'task-1',
        task_status: 'IN_PROGRESS',
        task_type: 'create',
        task_title: 'feat: viewport regression gate for DashboardHome operator widgets at 375px',
      },
    ],
    is_orphaned: false,
  },
  {
    pr_number: 4519,
    pr_url: 'https://github.com/example/repo/pull/4519',
    pr_title: 'orphaned PR with no associated task',
    pr_branch: 'blueprint/legacy-orphan',
    pr_created_at: '2026-05-06T08:00:00Z',
    tasks: [],
    is_orphaned: true,
  },
  {
    pr_number: 4520,
    pr_url: 'https://github.com/example/repo/pull/4520',
    pr_title: 'fix: residual remediation funnel stalled on REMEDIATING cases',
    pr_branch: 'blueprint/task-2',
    pr_created_at: '2026-05-07T11:30:00Z',
    tasks: [
      {
        task_id: 'task-2',
        task_status: 'EVALUATING',
        task_type: 'fix',
        task_title: 'fix: residual remediation funnel stalled on REMEDIATING cases',
      },
    ],
    is_orphaned: false,
  },
];

describe('PRMonitorSection — viewport regression', () => {
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

  test('expanded list renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PRMonitorSection
            openPRs={openPRs}
            loading={false}
            refreshing={false}
            onRefresh={() => {}}
            hasProject={true}
            tasks={tasks}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });

  test('expanded list renders without silent text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PRMonitorSection
            openPRs={openPRs}
            loading={false}
            refreshing={false}
            onRefresh={() => {}}
            hasProject={true}
            tasks={tasks}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });

  test('expanded list renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PRMonitorSection
            openPRs={openPRs}
            loading={false}
            refreshing={false}
            onRefresh={() => {}}
            hasProject={true}
            tasks={tasks}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });

  test('panel-review control renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PRMonitorSection
            openPRs={openPRs}
            loading={false}
            refreshing={false}
            onRefresh={() => {}}
            hasProject={true}
            tasks={tasks}
            onViewTask={() => {}}
            onSpawnFixTask={() => {}}
          />,
        );
      });
    });
  });

  test('empty state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PRMonitorSection
            openPRs={[]}
            loading={false}
            refreshing={false}
            onRefresh={() => {}}
            hasProject={true}
            tasks={[]}
            onViewTask={() => {}}
          />,
        );
      });
    });
  });
});
