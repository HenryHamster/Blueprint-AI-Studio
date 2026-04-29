import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ScopeTrendsSection } from './ScopeTrendsSection';
import type { ScopeBaselinesResponse, ScopeBaselinesSeriesPoint, ScopeBaselinesSummary } from './ScopeTrendsSection';

const { capturedChartData } = vi.hoisted(() => ({
  capturedChartData: { current: null as unknown[] | null },
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: ({ data, children }: { data: unknown[]; children: React.ReactNode }) => {
    capturedChartData.current = data;
    return <div data-testid="line-chart">{children}</div>;
  },
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

function makeSummary(trend: ScopeBaselinesSummary['trend'] = 'stable'): ScopeBaselinesSummary {
  return { merge_rate: 0.6, created: 10, merged: 6, trend };
}

function makePoint(overrides: Partial<ScopeBaselinesSeriesPoint>): ScopeBaselinesSeriesPoint {
  return { week: '2026-01-05', scope: 'small', created: 5, merged: 3, merge_rate: 0.6, ...overrides };
}

describe('ScopeTrendsSection', () => {
  beforeEach(() => {
    capturedChartData.current = null;
  });

  describe('loading state', () => {
    it('shows skeleton when loading=true and data=null', () => {
      const { container } = render(
        <ScopeTrendsSection data={null} loading={true} open={false} onToggle={() => {}} />
      );
      expect(container.querySelector('.animate-pulse')).not.toBeNull();
    });

    it('shows real content when loading=false and data arrives', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: makeSummary() },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />);
      expect(screen.getByRole('button')).toBeInTheDocument();
    });
  });

  describe('toggle button', () => {
    const data: ScopeBaselinesResponse = {
      series: [],
      summary: { small: makeSummary() },
      lookback_days: 30,
    };

    it('has aria-expanded="false" when open=false', () => {
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />);
      expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
    });

    it('has aria-expanded="true" when open=true', () => {
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'true');
    });

    it('calls onToggle when clicked', async () => {
      const onToggle = vi.fn();
      const user = userEvent.setup();
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={onToggle} />);
      await user.click(screen.getByRole('button'));
      expect(onToggle).toHaveBeenCalledOnce();
    });
  });

  describe('chart area visibility', () => {
    it('hides chart area when open=false', () => {
      const data: ScopeBaselinesResponse = {
        series: [makePoint({ week: '2026-01-05' }), makePoint({ week: '2026-01-12' })],
        summary: { small: makeSummary() },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />);
      expect(screen.queryByTestId('responsive-container')).toBeNull();
    });

    it('shows chart area when open=true and series has data', () => {
      const data: ScopeBaselinesResponse = {
        series: [makePoint({ week: '2026-01-05' }), makePoint({ week: '2026-01-12' })],
        summary: { small: makeSummary() },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  describe('summary trend badges', () => {
    it('renders badges in small/medium/large order regardless of input key order', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: {
          large: makeSummary('declining'),
          small: makeSummary('improving'),
          medium: makeSummary('stable'),
        },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />);
      const btn = screen.getByRole('button');
      const text = btn.textContent ?? '';
      expect(text.indexOf('small')).toBeLessThan(text.indexOf('medium'));
      expect(text.indexOf('medium')).toBeLessThan(text.indexOf('large'));
    });

    it('renders "—" when trend is null', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: { merge_rate: null, created: 0, merged: 0, trend: null } },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />);
      expect(screen.getByText(/—/)).toBeInTheDocument();
    });

    it('applies emerald color class for improving trend', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: makeSummary('improving') },
        lookback_days: 30,
      };
      const { container } = render(
        <ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />
      );
      expect(container.querySelector('.text-emerald-400')).not.toBeNull();
    });

    it('applies red color class for declining trend', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: makeSummary('declining') },
        lookback_days: 30,
      };
      const { container } = render(
        <ScopeTrendsSection data={data} loading={false} open={false} onToggle={() => {}} />
      );
      expect(container.querySelector('.text-red-400')).not.toBeNull();
    });
  });

  describe('week × scope pivot', () => {
    it('one row per unique sorted week, integer percents, null for missing combos', () => {
      const data: ScopeBaselinesResponse = {
        series: [
          makePoint({ week: '2026-01-12', scope: 'small', merge_rate: 0.8 }),
          makePoint({ week: '2026-01-05', scope: 'small', merge_rate: 0.6 }),
          makePoint({ week: '2026-01-05', scope: 'medium', merge_rate: 0.4 }),
          // no medium entry for 2026-01-12 → null
        ],
        summary: { small: makeSummary(), medium: makeSummary('improving') },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);

      const chartData = capturedChartData.current as Array<Record<string, string | number | null>>;
      expect(chartData).toHaveLength(2);
      expect(chartData[0].week).toBe('2026-01-05');
      expect(chartData[1].week).toBe('2026-01-12');
      expect(chartData[0].small).toBe(60);
      expect(chartData[0].medium).toBe(40);
      expect(chartData[1].small).toBe(80);
      expect(chartData[1].medium).toBeNull();
    });

    it('converts null merge_rate to null in chartData', () => {
      const data: ScopeBaselinesResponse = {
        series: [makePoint({ week: '2026-01-05', scope: 'small', merge_rate: null })],
        summary: { small: makeSummary() },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);

      const chartData = capturedChartData.current as Array<Record<string, string | number | null>>;
      expect(chartData[0].small).toBeNull();
    });
  });

  describe('empty state', () => {
    it('shows "No weekly data" message when open=true and series is empty', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: makeSummary() },
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByText('No weekly data in the last 30 days.')).toBeInTheDocument();
    });

    it('uses lookback_days from data in the empty message', () => {
      const data: ScopeBaselinesResponse = {
        series: [],
        summary: { small: makeSummary() },
        lookback_days: 14,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByText('No weekly data in the last 14 days.')).toBeInTheDocument();
    });

    it('falls back to 30 days when lookback_days is absent', () => {
      const data = { series: [], summary: {} } as unknown as ScopeBaselinesResponse;
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByText('No weekly data in the last 30 days.')).toBeInTheDocument();
    });

    it('shows empty state when summary is empty (no scopes) even with series entries', () => {
      const data: ScopeBaselinesResponse = {
        series: [makePoint({ week: '2026-01-05', scope: 'small' })],
        summary: {},
        lookback_days: 30,
      };
      render(<ScopeTrendsSection data={data} loading={false} open={true} onToggle={() => {}} />);
      expect(screen.getByText('No weekly data in the last 30 days.')).toBeInTheDocument();
    });
  });
});
