import { afterEach, describe, test, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import MetricsDashboard from './MetricsDashboard';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

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
  apiFetch: vi.fn(async () => ({
    ok: true,
    json: async () => ({
      axes: {
        cost_per_merged_pr: { current: 45.2, baseline: 50.0, drift_pct: -9.6 },
        vm_cold_start_p50_ms: { current: 3200, baseline: 3500, drift_pct: -8.6 },
        vm_reuse_rate: { current: 72, baseline: 68, drift_pct: 5.9 },
        median_api_calls_per_task: { current: 12, baseline: 15, drift_pct: -20.0 },
        median_review_iterations: { current: 2.1, baseline: 2.5, drift_pct: -16.0 },
      },
      baseline_captured_at: '2026-05-01T00:00:00Z',
      last_snapshot_at: '2026-05-08T00:00:00Z',
    }),
  })),
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
            tasks={[]}
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
            tasks={[]}
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
            tasks={[]}
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
            tasks={[]}
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
