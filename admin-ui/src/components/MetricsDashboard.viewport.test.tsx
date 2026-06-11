import { afterEach, describe, test, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import MetricsDashboard from './MetricsDashboard';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { Task } from '../types/task';

const now = Date.now();
const POPULATED_TASKS: Task[] = [
  {
    id: 'task-done-1',
    title: 'feat: add mobile-responsive grid layout to MetricsDashboard operator page',
    description: 'Refactor the MetricsDashboard grid to use Tailwind responsive breakpoints.',
    status: 'COMPLETED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 3_200_000).toISOString(),
    started_at: new Date(now - 3_000_000).toISOString(),
  },
  {
    id: 'task-done-2',
    title: 'fix: vm-reuse-rate widget overflows at 375px',
    description: 'Clamp the VM reuse rate widget width so it does not exceed the viewport.',
    status: 'COMPLETED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 2_400_000).toISOString(),
    started_at: new Date(now - 2_200_000).toISOString(),
  },
  {
    id: 'task-fail-1',
    title: 'fix: cost-per-merged-pr chart clips labels at mobile widths',
    description: 'Ensure X-axis labels in the cost chart do not clip below the chart boundary.',
    status: 'FAILED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 1_800_000).toISOString(),
    started_at: new Date(now - 1_600_000).toISOString(),
    failure_class: 'CODE_ERROR',
  },
  {
    id: 'task-active-1',
    title: 'perf: batch efficiency snapshot queries to reduce API call count',
    description: 'Combine the three separate efficiency snapshot fetches into one batched request.',
    status: 'IN_PROGRESS',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 900_000).toISOString(),
    started_at: new Date(now - 800_000).toISOString(),
  },
];

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null,
  Legend: () => null,
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/task-cost-breakdown')) {
      return new Response(JSON.stringify({
        project_id: 'test-project-id',
        window: '1h',
        window_ms: 3600000,
        window_start: '2026-04-24T11:00:00Z',
        task_count: 3,
        total_estimated_cost_usd: 48.75,
        task_type_breakdown: [
          { task_type: 'research', task_count: 2, estimated_cost_usd: 39.0 },
          { task_type: 'create', task_count: 1, estimated_cost_usd: 9.75 },
        ],
        direction_breakdown: [
          { direction_id: 'dir-super-long-direction-name-that-needs-wrap-001', task_count: 2, estimated_cost_usd: 39.0 },
          { direction_id: null, task_count: 1, estimated_cost_usd: 9.75 },
        ],
        top_tasks: [
          {
            task_id: 'task-1',
            task_title: 'Investigate the deeply nested metrics dashboard cost-overview panel for wrap safety at mobile widths',
            task_type: 'research',
            direction_id: 'dir-super-long-direction-name-that-needs-wrap-001',
            completed_at: '2026-04-24T11:55:00Z',
            estimated_cost_usd: 25.25,
          },
          {
            task_id: 'task-2',
            task_title: 'Refine dashboard cost breakdown aggregation and keep the long labels from clipping',
            task_type: 'create',
            direction_id: null,
            completed_at: '2026-04-24T11:40:00Z',
            estimated_cost_usd: 9.75,
          },
        ],
      }), { status: 200 });
    }
    if (url.includes('/lane-diag')) {
      return new Response(JSON.stringify({
        pr_count: 3,
        task_count: 12,
        window_days: 7,
        truncated: false,
        pr_breakdown_truncated: false,
        relay_counts_by_lane: { 'ci-repair': 2, 'review-rework': 1 },
        lane_waste: {
          waste_taxonomy: { infra: 2, merge_conflict: 1, ci_churn: 3, review_ambiguity: 0, lost_reasoning: 1, unknown: 0 },
        },
        per_pr_phase_breakdown: [],
      }), { status: 200 });
    }
    return new Response(JSON.stringify({
      axes: {
        cost_per_merged_pr: {
          current: 45.2, baseline: 50.0, drift_pct: -9.6,
          history: [
            { captured_at: '2026-03-01T00:00:00Z', value: 54.0 },
            { captured_at: '2026-03-08T00:00:00Z', value: 51.0 },
            { captured_at: '2026-04-01T00:00:00Z', value: 50.0 },
            { captured_at: '2026-05-01T00:00:00Z', value: 45.2 },
          ],
        },
        vm_cold_start_p50_ms: {
          current: 3200, baseline: 3500, drift_pct: -8.6,
          history: [
            { captured_at: '2026-03-01T00:00:00Z', value: 3600 },
            { captured_at: '2026-04-01T00:00:00Z', value: 3500 },
            { captured_at: '2026-05-01T00:00:00Z', value: 3200 },
          ],
        },
        vm_reuse_rate: { current: 72, baseline: 68, drift_pct: 5.9, history: [] },
        median_api_calls_per_task: { current: 12, baseline: 15, drift_pct: -20.0, history: [{ captured_at: '2026-05-01T00:00:00Z', value: 12 }] },
        median_review_iterations: { current: 2.1, baseline: 2.5, drift_pct: -16.0 },
      },
      baseline_captured_at: '2026-05-01T00:00:00Z',
      last_snapshot_at: '2026-05-08T00:00:00Z',
    }), { status: 200 });
  }),
}));

describe('MetricsDashboard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

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

  test('renders window selector without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MetricsDashboard
            metricsHistory={[]}
            cumulativeCost={0}
            cumulativeVmHours={0}
            maxConcurrentVMs={4}
            tasks={POPULATED_TASKS}
            metricsWindow="1h"
            onWindowChange={() => {}}
          />,
        );
      });
    });
  });

  test('operator-facing text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MetricsDashboard
            metricsHistory={[]}
            cumulativeCost={12.34}
            cumulativeVmHours={5.67}
            maxConcurrentVMs={4}
            tasks={POPULATED_TASKS}
            metricsWindow="1h"
            onWindowChange={() => {}}
          />,
        );
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MetricsDashboard
            metricsHistory={[]}
            cumulativeCost={0}
            cumulativeVmHours={0}
            maxConcurrentVMs={4}
            tasks={POPULATED_TASKS}
            metricsWindow="1h"
            onWindowChange={() => {}}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });

  test('efficiency widget renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MetricsDashboard
            metricsHistory={[]}
            cumulativeCost={0}
            cumulativeVmHours={0}
            maxConcurrentVMs={4}
            tasks={POPULATED_TASKS}
            metricsWindow="1h"
            onWindowChange={() => {}}
            projectId="test-project-id"
          />,
        );
      });
      // Wait for the efficiency data to load
      await new Promise(resolve => setTimeout(resolve, 100));
    });
  });
});
