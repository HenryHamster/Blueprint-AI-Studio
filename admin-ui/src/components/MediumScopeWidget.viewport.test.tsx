import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MediumScopeWidget, type MediumScopeMeasurement } from './MediumScopeWidget';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
}));

function makeWindow(merge_rate: number, merged: number, created: number) {
  return {
    merge_rate,
    created,
    merged,
    window_start: '2026-04-01T00:00:00Z',
    window_end: '2026-05-01T00:00:00Z',
  };
}

const data: MediumScopeMeasurement = {
  medium_scope_conversion_rate: 0.42,
  total_created: 1242,
  total_merged: 521,
  investment_start: '2026-03-01T00:00:00Z',
  pre_investment: makeWindow(0.31, 124, 400),
  post_investment: makeWindow(0.48, 397, 842),
  recent_30d: makeWindow(0.51, 184, 360),
  recent_7d: makeWindow(0.54, 47, 87),
  recent_3d: makeWindow(0.49, 17, 35),
  recent_24h: makeWindow(0.6, 6, 10),
  weekly_series: [],
  daily_series: [
    { day: '2026-04-25', created: 18, merged: 9, merge_rate: 0.5, direction_merges: 4, merge_rate_delta: 0.04 },
    { day: '2026-04-26', created: 22, merged: 11, merge_rate: 0.5, direction_merges: 5, merge_rate_delta: 0 },
    { day: '2026-04-27', created: 25, merged: 14, merge_rate: 0.56, direction_merges: 6, merge_rate_delta: 0.06 },
  ],
  correlated_changes: [
    {
      title: 'feat: medium-scope task templates with explicit related_files lists and very long descriptive title',
      week: '2026-04-22',
      merge_rate_delta: 0.07,
    },
    {
      title: 'fix: scope inflation classifier stricter on TYPESCRIPT_REFACTOR triggers',
      week: '2026-04-15',
      merge_rate_delta: -0.04,
    },
  ],
};

describe('MediumScopeWidget — viewport regression', () => {
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

  test('renders headline + chart without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MediumScopeWidget data={data} loading={false} />);
      });
    });
  });

  test('renders headline + chart without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MediumScopeWidget data={data} loading={false} />);
      });
    });
  });

  test('renders loading skeleton without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MediumScopeWidget data={null} loading={true} />);
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
        r.render(<MediumScopeWidget data={null} loading={false} />);
      });
    });
  });
});
