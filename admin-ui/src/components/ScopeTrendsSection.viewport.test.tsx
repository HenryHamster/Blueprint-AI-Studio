import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ScopeTrendsSection, type ScopeBaselinesResponse } from './ScopeTrendsSection';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

const data: ScopeBaselinesResponse = {
  series: [
    { week: '2026-W14', scope: 'small', created: 30, merged: 22, merge_rate: 0.73 },
    { week: '2026-W15', scope: 'small', created: 35, merged: 24, merge_rate: 0.69 },
    { week: '2026-W16', scope: 'small', created: 42, merged: 30, merge_rate: 0.71 },
    { week: '2026-W14', scope: 'medium', created: 18, merged: 7, merge_rate: 0.39 },
    { week: '2026-W15', scope: 'medium', created: 20, merged: 9, merge_rate: 0.45 },
    { week: '2026-W16', scope: 'medium', created: 22, merged: 8, merge_rate: 0.36 },
    { week: '2026-W14', scope: 'large', created: 5, merged: 1, merge_rate: 0.2 },
    { week: '2026-W15', scope: 'large', created: 7, merged: 1, merge_rate: 0.14 },
    { week: '2026-W16', scope: 'large', created: 6, merged: 0, merge_rate: 0.0 },
  ],
  summary: {
    small: { merge_rate: 0.71, created: 107, merged: 76, trend: 'stable' },
    medium: { merge_rate: 0.4, created: 60, merged: 24, trend: 'improving' },
    large: { merge_rate: 0.11, created: 18, merged: 2, trend: 'declining' },
  },
  lookback_days: 30,
};

describe('ScopeTrendsSection — viewport regression', () => {
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

  test('open state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />,
        );
      });
    });
  });

  test('open state renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />,
        );
      });
    });
  });

  test('closed state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />,
        );
      });
    });
  });
});
