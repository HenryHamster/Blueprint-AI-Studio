import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MetricCard,
  FailedCard,
  SuccessRateCard,
  ConversionRateCard,
  AcceptanceRateCard,
  CostCard,
  SrCatchRateCard,
  deriveReviewerFindingCount,
} from './DashboardMetricCards';
import type { SparkPoint, FailureGroup } from './DashboardHome';
import type { SrTrendWithFixIterations } from './ReviewQualitySection';

// recharts renders an SVG with sizing that jsdom cannot measure; stub it so the
// spark area is observable without a real layout (mirrors DashboardHome.test.tsx).
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="spark">{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
}));

const sparkOf = (n: number): SparkPoint[] =>
  Array.from({ length: n }, (_, i) => ({ active: i, queued: i, failed: i }));

describe('MetricCard', () => {
  it('renders label and value', () => {
    render(<MetricCard label="Active" value={42} color="emerald" spark={[]} dataKey="active" loading={false} />);
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  it('renders a sparkline only when there are at least 3 points', () => {
    const { rerender } = render(
      <MetricCard label="Active" value={1} color="emerald" spark={sparkOf(2)} dataKey="active" loading={false} />,
    );
    expect(screen.queryByTestId('spark')).not.toBeInTheDocument();
    rerender(<MetricCard label="Active" value={1} color="emerald" spark={sparkOf(3)} dataKey="active" loading={false} />);
    expect(screen.getByTestId('spark')).toBeInTheDocument();
  });

  it('renders a skeleton while loading', () => {
    const { container } = render(
      <MetricCard label="Active" value={1} color="emerald" spark={sparkOf(3)} dataKey="active" loading={true} />,
    );
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });
});

describe('FailedCard', () => {
  it('renders the Failed (24h) label, value and top-3 failure-class pills', () => {
    const groups: FailureGroup[] = [
      { failure_class: 'CODE_ERROR', label: 'Code', count: 5, tasks: [] },
      { failure_class: 'RATE_LIMIT', label: 'Rate', count: 3, tasks: [] },
      { failure_class: 'AGENT_TIMEOUT', label: 'Timeout', count: 2, tasks: [] },
      { failure_class: 'EXTERNAL_API', label: 'Ext', count: 1, tasks: [] },
    ];
    render(<FailedCard value={11} groups={groups} spark={[]} loading={false} windowHours={24} />);
    expect(screen.getByText('Failed (24h)')).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
    expect(screen.getByText('5 code')).toBeInTheDocument();
    expect(screen.getByText('3 rate')).toBeInTheDocument();
    expect(screen.getByText('2 timeout')).toBeInTheDocument();
    // 4th group is dropped (top-3 only)
    expect(screen.queryByText('1 ext api')).not.toBeInTheDocument();
  });

  it('hides failure-class pills when value is 0', () => {
    const groups: FailureGroup[] = [{ failure_class: 'CODE_ERROR', label: 'Code', count: 5, tasks: [] }];
    render(<FailedCard value={0} groups={groups} spark={[]} loading={false} />);
    expect(screen.queryByText('5 code')).not.toBeInTheDocument();
  });

  it('label reflects the selected window (7d when windowHours=168)', () => {
    render(<FailedCard value={3} spark={[]} loading={false} windowHours={168} />);
    expect(screen.getByText('Failed (7d)')).toBeInTheDocument();
  });
});

describe('SuccessRateCard', () => {
  it('renders -- when rate is null', () => {
    render(<SuccessRateCard rate={null} loading={false} />);
    expect(screen.getByText('--')).toBeInTheDocument();
  });

  it('renders the rounded percentage', () => {
    render(<SuccessRateCard rate={0.756} trend="improving" loading={false} />);
    expect(screen.getByText('76%')).toBeInTheDocument();
  });
});

describe('ConversionRateCard', () => {
  it('renders -- and neutral color when rate is null', () => {
    render(<ConversionRateCard rate={null} loading={false} />);
    expect(screen.getByText('--')).toHaveClass('text-neutral-500');
  });

  it('colors the value green at or above 60%', () => {
    render(<ConversionRateCard rate={0.6} loading={false} />);
    expect(screen.getByText('60%')).toHaveClass('text-emerald-400');
  });

  it('colors the value red below 30%', () => {
    render(<ConversionRateCard rate={0.1} loading={false} />);
    expect(screen.getByText('10%')).toHaveClass('text-red-400');
  });

  it('labels the denominator as PR-eligible tasks, not all tasks', () => {
    render(<ConversionRateCard rate={0.6} loading={false} />);
    expect(screen.getByText('PRs merged / PR-eligible tasks created')).toBeInTheDocument();
    expect(screen.queryByText('PRs merged / tasks created')).toBeNull();
  });
});

describe('AcceptanceRateCard', () => {
  it('colors the value amber between 30% and 60%', () => {
    render(<AcceptanceRateCard rate={0.45} loading={false} />);
    expect(screen.getByText('45%')).toHaveClass('text-amber-400');
  });
});

describe('CostCard', () => {
  it('renders -- when totalCost is null', () => {
    render(<CostCard totalCost={null} loading={false} />);
    expect(screen.getByText('--')).toBeInTheDocument();
  });

  it('label reflects the selected window (7d when windowHours=168)', () => {
    render(<CostCard totalCost={5} loading={false} windowHours={168} />);
    expect(screen.getByText('Cost (7d)')).toBeInTheDocument();
  });

  it('renders two decimals below $10', () => {
    render(<CostCard totalCost={4.2} loading={false} />);
    expect(screen.getByText('$4.20')).toBeInTheDocument();
  });

  it('rounds to a whole dollar at or above $10', () => {
    render(<CostCard totalCost={123.7} loading={false} />);
    expect(screen.getByText('$124')).toBeInTheDocument();
  });
});

describe('deriveReviewerFindingCount', () => {
  it('returns 0 when there are no categories', () => {
    expect(deriveReviewerFindingCount(null)).toBe(0);
    expect(deriveReviewerFindingCount(undefined)).toBe(0);
  });

  it('sums current_reviewer_finding_count across categories', () => {
    const trends = {
      categories: {
        correctness: { current_reviewer_finding_count: 4 },
        security: { current_reviewer_finding_count: 3 },
      },
    } as unknown as SrTrendWithFixIterations;
    expect(deriveReviewerFindingCount(trends)).toBe(7);
  });

  it('falls back to current_human_count for backward compatibility', () => {
    const trends = {
      categories: {
        correctness: { current_human_count: 4 },
      },
    } as unknown as SrTrendWithFixIterations;
    expect(deriveReviewerFindingCount(trends)).toBe(4);
  });
});

describe('SrCatchRateCard', () => {
  const trendsWithFindings = (reviewerFindingCount: number, extra: Partial<SrTrendWithFixIterations> = {}) =>
    ({
      categories: { correctness: { current_reviewer_finding_count: reviewerFindingCount } },
      ...extra,
    }) as unknown as SrTrendWithFixIterations;

  it('shows "too small" and fires the scroll callback when findings are below the threshold', async () => {
    const onScroll = vi.fn();
    render(
      <SrCatchRateCard rate={0.8} trends={trendsWithFindings(4)} loading={false} onScrollToReviewQuality={onScroll} />,
    );
    expect(screen.getByText('too small')).toBeInTheDocument();
    expect(screen.getByText('n=4 findings')).toBeInTheDocument();
    await userEvent.click(screen.getByText('too small'));
    expect(onScroll).toHaveBeenCalledTimes(1);
  });

  it('renders the catch-rate percentage when findings meet the threshold', () => {
    render(<SrCatchRateCard rate={0.82} trends={trendsWithFindings(10)} loading={false} />);
    expect(screen.getByText('82%')).toBeInTheDocument();
    expect(screen.getByText('n=10 findings')).toBeInTheDocument();
  });

  it('shows the stale badge when trend data is stale', () => {
    render(<SrCatchRateCard rate={0.82} trends={trendsWithFindings(10, { is_stale: true })} loading={false} />);
    expect(screen.getByText('stale')).toBeInTheDocument();
  });

  it('shows the frozen badge when data is insufficient and not stale', () => {
    render(
      <SrCatchRateCard rate={0.82} trends={trendsWithFindings(10, { data_sufficient: false })} loading={false} />,
    );
    expect(screen.getByText('frozen')).toBeInTheDocument();
  });
});
