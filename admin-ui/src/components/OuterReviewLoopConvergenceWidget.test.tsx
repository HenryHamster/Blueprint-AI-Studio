import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, type ComponentProps } from 'react';
import { OuterReviewLoopConvergenceWidget } from './OuterReviewLoopConvergenceWidget';

const { fetchOuterLoopConvergenceMock, fetchOuterLoopConvergenceTrendMock } = vi.hoisted(() => ({
  fetchOuterLoopConvergenceMock: vi.fn(),
  fetchOuterLoopConvergenceTrendMock: vi.fn(),
}));

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
    {
      date: '2026-05-01',
      pr_count: 4,
      convergence_rate: 0.5,
      escalation_total: 1,
      churn_total: 2,
      terminal_outcome_histogram: { merged: 2, rejected: 1, stuck: 1 },
      incidence_counts: { churn: 2, overall_escalations: 1 },
      incidence_rates: { churn: 0.5, overall_escalations: 0.25 },
    },
    {
      date: '2026-05-02',
      pr_count: 5,
      convergence_rate: 0.8,
      escalation_total: 0,
      churn_total: 1,
      terminal_outcome_histogram: { merged: 4, rejected: 1 },
      incidence_counts: { churn: 1, overall_escalations: 0 },
      incidence_rates: { churn: 0.2, overall_escalations: 0 },
    },
  ],
};

vi.mock('../api', () => ({
  fetchOuterLoopConvergence: (...args: unknown[]) => fetchOuterLoopConvergenceMock(...args),
  fetchOuterLoopConvergenceTrend: (...args: unknown[]) => fetchOuterLoopConvergenceTrendMock(...args),
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
  terminal_outcome_histogram: {
    merged: 5,
    rejected: 2,
    cap_exhausted: 1,
    stuck: 1,
    standoff: 1,
    repeat_fingerprint_halt: 1,
  },
  incidence_counts: { churn: 2, stuck: 1, standoff: 1, repeat_fingerprint: 1, overall_escalations: 3 },
  incidence_rates: { churn: 0.222, stuck: 0.111, standoff: 0.111, repeat_fingerprint: 0.111, overall_escalations: 0.333 },
  escalation_total: 3,
  escalation_by_reason: { stuck: 2, standoff: 1, 'repeat-fingerprint': 0 },
  convergence_rate: 0.625,
};

fetchOuterLoopConvergenceMock.mockImplementation(async () => summaryPayload);
fetchOuterLoopConvergenceTrendMock.mockImplementation(async () => TREND);

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

async function flushWidget(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 60));
  });
}

async function renderWidget(
  props: ComponentProps<typeof OuterReviewLoopConvergenceWidget> = {},
): Promise<{ root: Root; host: HTMLDivElement }> {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<OuterReviewLoopConvergenceWidget {...props} />);
  });
  await flushWidget();
  return { root, host };
}

describe('OuterReviewLoopConvergenceWidget', () => {
  let root: Root | null = null;
  let host: HTMLElement | null = null;

  afterEach(() => {
    fetchOuterLoopConvergenceMock.mockReset();
    fetchOuterLoopConvergenceTrendMock.mockReset();
    fetchOuterLoopConvergenceMock.mockImplementation(async () => summaryPayload);
    fetchOuterLoopConvergenceTrendMock.mockImplementation(async () => TREND);
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

    expect(text).toContain('63%');
    expect(text).toContain('9 PR cycles');
    expect(text).toContain('p95 cycles');
    expect(text).toContain('cap-exhausted');
    expect(text).toContain('stuck');
    expect(text).toContain('standoff');
    expect(text).toContain('repeat-fp halt');
    expect(text).toContain('2 · 22%');
    expect(text).toContain('3 · 33%');
    expect(text).toContain('2026-05-02 · 5 PRs · churn 1 · escalations 0');
  });

  test('renders nothing when there are no PRs with a review loop', async () => {
    summaryPayload = { ...POPULATED, prs_with_review_loop: 0 };
    const rendered = await renderWidget();
    root = rendered.root;
    host = rendered.host;
    expect(host.textContent).toBe('');
  });

  test('falls back to legacy outcomes and derived incidence rates when additive fields are absent', async () => {
    summaryPayload = {
      ...POPULATED,
      terminal_outcome_histogram: undefined,
      incidence_counts: undefined,
      incidence_rates: undefined,
    };
    const rendered = await renderWidget();
    root = rendered.root;
    host = rendered.host;
    const text = host.textContent ?? '';

    expect(text).toContain('rejected1');
    expect(text).toContain('cap-exhausted');
    expect(text).toContain('repeat-fp halt');
    expect(text).toContain('2 · 22%');
    expect(text).toContain('3 · 33%');
  });

  test('ignores stale responses after project filters change mid-request', async () => {
    const slowSummary = deferred<typeof POPULATED>();
    const slowTrend = deferred<typeof TREND>();
    const freshSummary = deferred<typeof POPULATED>();
    const freshTrend = deferred<typeof TREND>();
    const alpha = {
      ...POPULATED,
      window_hours: 24,
      prs_with_review_loop: 4,
      convergence_rate: 0.25,
    };
    const beta = {
      ...POPULATED,
      window_hours: 48,
      prs_with_review_loop: 11,
      convergence_rate: 0.875,
    };
    const betaTrend = {
      ...TREND,
      buckets: [
        ...TREND.buckets.slice(0, -1),
        {
          ...TREND.buckets[TREND.buckets.length - 1],
          date: '2026-05-09',
          pr_count: 11,
          convergence_rate: 0.875,
        },
      ],
    };
    let staleSignal: AbortSignal | undefined;

    fetchOuterLoopConvergenceMock
      .mockImplementationOnce((...args: unknown[]) => {
        staleSignal = args[2] as AbortSignal | undefined;
        return slowSummary.promise;
      })
      .mockImplementationOnce(async () => freshSummary.promise);
    fetchOuterLoopConvergenceTrendMock
      .mockImplementationOnce(async () => slowTrend.promise)
      .mockImplementationOnce(async () => freshTrend.promise);

    const rendered = await renderWidget({ projectId: 'alpha', windowHours: 24 });
    root = rendered.root;
    host = rendered.host;

    await act(async () => {
      root?.render(<OuterReviewLoopConvergenceWidget projectId="beta" windowHours={48} />);
    });

    expect(staleSignal?.aborted).toBe(true);

    freshSummary.resolve(beta);
    freshTrend.resolve(betaTrend);
    await flushWidget();

    expect(host.textContent).toContain('88%');
    expect(host.textContent).toContain('11 PR cycles');
    expect(host.textContent).toContain('48h');
    expect(host.textContent).toContain('2026-05-09 · 11 PRs · churn 1 · escalations 0');

    slowSummary.resolve(alpha);
    slowTrend.resolve(TREND);
    await flushWidget();

    expect(host.textContent).toContain('88%');
    expect(host.textContent).toContain('11 PR cycles');
    expect(host.textContent).not.toContain('25%');
    expect(host.textContent).not.toContain('4 PR cycles · 24h');
  });
});
