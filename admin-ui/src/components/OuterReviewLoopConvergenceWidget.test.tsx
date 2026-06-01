import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OuterReviewLoopConvergenceWidget } from './OuterReviewLoopConvergenceWidget';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Tooltip: () => null,
}));

// Mutable summary payload swapped per test; the trend payload is constant.
let summaryPayload: unknown = {};

const TREND = {
  days: 7,
  buckets: [
    { date: '2026-05-01', pr_count: 4, convergence_rate: 0.5, escalation_total: 1, churn_total: 2 },
    { date: '2026-05-02', pr_count: 5, convergence_rate: 0.8, escalation_total: 0, churn_total: 1 },
  ],
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/outer-loop-convergence-trend')) {
      return new Response(JSON.stringify(TREND), { status: 200 });
    }
    if (url.includes('/outer-loop-convergence')) {
      return new Response(JSON.stringify(summaryPayload), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
}));

const POPULATED = {
  window_hours: 24,
  prs_with_review_loop: 9,
  iteration_median: 2,
  iteration_p95: 5,
  iteration_distribution: { '1': 3, '2': 3, '3': 2, '4+': 1 },
  churn_total: 4,
  churn_pr_count: 2,
  drift_pr_count: 1,
  outcomes: { merged: 5, rejected: 2, cap_exhausted: 1, repeat_fingerprint_halt: 1 },
  escalation_total: 3,
  escalation_by_reason: { stuck: 2, standoff: 1, 'repeat-fingerprint': 0 },
  convergence_rate: 0.625,
};

async function renderWidget(): Promise<{ root: Root; host: HTMLDivElement }> {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<OuterReviewLoopConvergenceWidget />);
  });
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 60));
  });
  return { root, host };
}

describe('OuterReviewLoopConvergenceWidget', () => {
  let root: Root | null = null;
  let host: HTMLElement | null = null;

  afterEach(() => {
    if (root) {
      const r = root;
      act(() => r.unmount());
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders convergence headline, outcomes, and escalation reasons when populated', async () => {
    summaryPayload = POPULATED;
    const rendered = await renderWidget();
    root = rendered.root;
    host = rendered.host;
    const text = host.textContent ?? '';
    // convergence_rate 0.625 -> 63%
    expect(text).toContain('63%');
    expect(text).toContain('converged (merged)');
    // outcome counts surfaced
    expect(text).toContain('cap-exhausted');
    expect(text).toContain('repeat-fp halt');
    // escalation reason chips (only nonzero reasons render)
    expect(text).toContain('stuck');
    expect(text).toContain('standoff');
    expect(text).toContain('9 PRs');
  });

  test('renders nothing when there are no PRs with a review loop', async () => {
    summaryPayload = { ...POPULATED, prs_with_review_loop: 0 };
    const rendered = await renderWidget();
    root = rendered.root;
    host = rendered.host;
    expect(host.textContent).toBe('');
  });
});
