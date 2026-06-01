import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WarmReuseVsCostTrend } from './WarmReuseVsCostTrend';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
  ReferenceLine: () => null,
}));

vi.mock('../api', () => ({
  fetchEfficiencyAxes: vi.fn(async () => ({
    axes: {
      vm_reuse_rate: {
        current: 42,
        baseline: 18,
        drift_pct: 133.3,
        history: [
          { captured_at: '2026-05-01T00:00:00', value: 20 },
          { captured_at: '2026-05-02T00:00:00', value: 30 },
          { captured_at: '2026-05-03T00:00:00', value: 42 },
        ],
      },
      cost_per_merged_pr: {
        current: 3.1,
        baseline: 3.0,
        drift_pct: 3.3,
        history: [
          { captured_at: '2026-05-01T00:00:00', value: 3.2 },
          { captured_at: '2026-05-02T00:00:00', value: 3.0 },
          { captured_at: '2026-05-03T00:00:00', value: 3.1 },
        ],
      },
    },
    baseline_captured_at: '2026-04-24T00:00:00',
    last_snapshot_at: '2026-05-03T00:00:00',
  })),
}));

describe('WarmReuseVsCostTrend — viewport regression', () => {
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

  async function mountAt() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<WarmReuseVsCostTrend projectId="proj-1" />);
    });
    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 80));
    });
  }

  test('renders trend without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mountAt);
  });

  test('renders trend without text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, mountAt);
  });
});
