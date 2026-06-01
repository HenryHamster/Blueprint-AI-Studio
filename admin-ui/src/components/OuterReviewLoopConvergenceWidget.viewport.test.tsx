import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OuterReviewLoopConvergenceWidget } from './OuterReviewLoopConvergenceWidget';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Tooltip: () => null,
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/outer-loop-convergence-trend')) {
      return new Response(
        JSON.stringify({
          days: 7,
          buckets: [
            { date: '2026-05-01', pr_count: 4, convergence_rate: 0.5, escalation_total: 1, churn_total: 2 },
            { date: '2026-05-02', pr_count: 5, convergence_rate: 0.8, escalation_total: 0, churn_total: 1 },
            { date: '2026-05-03', pr_count: 6, convergence_rate: 0.7, escalation_total: 2, churn_total: 3 },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.includes('/outer-loop-convergence')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          prs_with_review_loop: 12,
          iteration_median: 2,
          iteration_p95: 6,
          iteration_distribution: { '1': 4, '2': 4, '3': 2, '4+': 2 },
          churn_total: 5,
          churn_pr_count: 3,
          drift_pr_count: 2,
          outcomes: { merged: 7, rejected: 3, cap_exhausted: 1, repeat_fingerprint_halt: 1 },
          escalation_total: 4,
          escalation_by_reason: { stuck: 2, standoff: 1, 'repeat-fingerprint': 1 },
          convergence_rate: 0.58,
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

describe('OuterReviewLoopConvergenceWidget — viewport regression', () => {
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

  async function mountAt() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<OuterReviewLoopConvergenceWidget />);
    });
    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 80));
    });
  }

  test('renders stat grid + trend without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mountAt);
  });

  test('renders stat grid + trend without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, mountAt);
  });

  test('has no undocumented small touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, mountAt);
  });
});
