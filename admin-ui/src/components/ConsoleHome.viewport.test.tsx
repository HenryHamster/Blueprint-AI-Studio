import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ConsoleHome } from './ConsoleHome';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import type { Task } from '../types/task';

// ConsoleHome (via useDashboardDigest and fetchFleetUtilization) imports apiFetch
// from '../api', which transitively pulls in Firebase. Mock it so the test does
// not require real auth and fetch calls resolve immediately.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/digest')) {
      return new Response(
        JSON.stringify({
          failed_count: 2,
          pending_approval_count: 1,
          direction_progress: [],
          recent_completions: [],
          failure_groups: [],
          success_rate: 0.88,
          trend: 'stable',
          trend_history: [],
          completed_count: 10,
          needs_attention: [
            {
              task_id: 'task-approval-1',
              title: 'compute_experiment: benchmark new inference pipeline on GPU cluster',
              reason: 'approval_required',
              spend_cap_usd: 250.0,
              deadline_seconds: 86400,
              waiting_hours: 2,
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.includes('/fleet-utilization')) {
      return new Response(JSON.stringify({ pools: [] }), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
  fetchFleetUtilization: vi.fn(async () => ({ pools: [] })),
  // ConsoleHome statically imports fetchTask from '../api' (used in the
  // onViewTask click path). In browser-mode ESM, the mock factory must
  // expose every named export the component imports or the module linker
  // throws "does not provide an export named 'fetchTask'" at import time.
  fetchTask: vi.fn(async () => ({})),
  // Same linker requirement: ConsoleHome statically imports triggerSelfAudit
  // (the self-audit button click path), so the mock must expose it too.
  triggerSelfAudit: vi.fn(async () => ({ task_id: 'audit-task-test' })),
  API_BASE_URL: '',
  authGet: vi.fn(async () => ({})),
}));

const TASKS: Task[] = [
  {
    id: 'task-1',
    title: 'feat: add mobile-responsive layout for operator panels across all screen sizes',
    description: '',
    status: 'IN_PROGRESS',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(Date.now() - 3_600_000).toISOString(),
    started_at: new Date(Date.now() - 3_500_000).toISOString(),
  },
  {
    id: 'task-2',
    title: 'fix: viewport overflow in kanban card when title exceeds two lines',
    description: '',
    status: 'QUEUED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(Date.now() - 1_800_000).toISOString(),
  },
];

describe('ConsoleHome — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
  });

  function render(r: Root) {
    r.render(
      <ConsoleHome
        selectedProjectId={null}
        scopedActiveCount={null}
        scopedQueuedCount={null}
        globalActiveCount={1}
        globalQueuedCount={2}
        tasks={TASKS}
        onViewTask={() => {}}
        onShowAnalytics={() => {}}
        isAdmin={true}
      />,
    );
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { render(root!); });
    });
  });

  test('operator text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { render(root!); });
    });
  });

  test('action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { render(root!); });
    });
  });

  test('NeedsAttentionSection renders spend cap and deadline at 1280px without overflow', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { render(root!); });
      // Verify spend cap and deadline text are present
      const capText = host.textContent ?? '';
      if (!capText.includes('$250.00')) throw new Error('spend_cap_usd not rendered');
      if (!capText.includes('24h')) throw new Error('deadline_seconds not rendered');
    });
  });
});
