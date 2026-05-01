import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MediumScopeWidget } from './MediumScopeWidget';
import type { MediumScopeMeasurement, WindowStats, CorrelatedChange, DailySeries } from './MediumScopeWidget';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="line-chart">{children}</div>
  ),
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
}));

function makeWindowStats(merge_rate: number | null = 0.6, created = 10, merged = 6): WindowStats {
  return { merge_rate, created, merged, window_start: null, window_end: null };
}

function makeDailySeries(n: number): DailySeries[] {
  return Array.from({ length: n }, (_, i) => ({
    day: `2026-01-${String(i + 1).padStart(2, '0')}`,
    created: 5,
    merged: 3,
    merge_rate: 0.6,
    direction_merges: 0,
    merge_rate_delta: null,
  }));
}

function makeData(overrides: Partial<MediumScopeMeasurement> = {}): MediumScopeMeasurement {
  return {
    medium_scope_conversion_rate: 0.6,
    total_created: 10,
    total_merged: 6,
    investment_start: null,
    pre_investment: null,
    post_investment: null,
    recent_30d: null,
    recent_7d: null,
    recent_3d: null,
    recent_24h: null,
    weekly_series: [],
    daily_series: [],
    correlated_changes: [],
    ...overrides,
  };
}

describe('MediumScopeWidget', () => {
  describe('loading and null states', () => {
    it('shows skeleton when loading=true', () => {
      const { container } = render(<MediumScopeWidget data={null} loading={true} />);
      expect(container.querySelector('.animate-pulse')).not.toBeNull();
    });

    it('shows fallback text when data is null', () => {
      render(<MediumScopeWidget data={null} loading={false} />);
      expect(screen.getByText('No medium-scope task data yet')).toBeInTheDocument();
    });

    it('shows fallback text when medium_scope_conversion_rate is null', () => {
      render(<MediumScopeWidget data={makeData({ medium_scope_conversion_rate: null })} loading={false} />);
      expect(screen.getByText('No medium-scope task data yet')).toBeInTheDocument();
    });
  });

  describe('headline rate', () => {
    it('shows rate as rounded percent', () => {
      render(<MediumScopeWidget data={makeData({ medium_scope_conversion_rate: 0.654 })} loading={false} />);
      expect(screen.getByText('65%')).toBeInTheDocument();
    });

    it('applies text-emerald-400 when rate >= 0.6', () => {
      const { container } = render(
        <MediumScopeWidget data={makeData({ medium_scope_conversion_rate: 0.75 })} loading={false} />
      );
      expect(container.querySelector('.text-emerald-400.font-bold')).not.toBeNull();
    });

    it('applies text-amber-400 when 0.3 <= rate < 0.6', () => {
      const { container } = render(
        <MediumScopeWidget data={makeData({ medium_scope_conversion_rate: 0.45 })} loading={false} />
      );
      expect(container.querySelector('.text-amber-400.font-bold')).not.toBeNull();
    });

    it('applies text-red-400 when rate < 0.3', () => {
      const { container } = render(
        <MediumScopeWidget data={makeData({ medium_scope_conversion_rate: 0.2 })} loading={false} />
      );
      expect(container.querySelector('.text-red-400.font-bold')).not.toBeNull();
    });
  });

  describe('pre/post investment delta message', () => {
    it('shows "No significant change" with pp value when |deltaPp| <= 3', () => {
      // post 62% - pre 60% = +2pp (within 3pp)
      render(
        <MediumScopeWidget
          data={makeData({
            pre_investment: makeWindowStats(0.60),
            post_investment: makeWindowStats(0.62),
          })}
          loading={false}
        />
      );
      expect(
        screen.getByText('No significant change detected (within 3pp, +2pp)')
      ).toBeInTheDocument();
    });

    it('shows improved message in emerald when deltaPp > 3', () => {
      // post 60% - pre 50% = +10pp
      render(
        <MediumScopeWidget
          data={makeData({
            pre_investment: makeWindowStats(0.50),
            post_investment: makeWindowStats(0.60),
          })}
          loading={false}
        />
      );
      const msgEl = screen.getByText(/improved by 10 percentage points/);
      expect(msgEl).toBeInTheDocument();
      expect(msgEl.className).toContain('text-emerald-400');
    });

    it('shows declined message in red when deltaPp < -3', () => {
      // post 50% - pre 60% = -10pp
      render(
        <MediumScopeWidget
          data={makeData({
            pre_investment: makeWindowStats(0.60),
            post_investment: makeWindowStats(0.50),
          })}
          loading={false}
        />
      );
      const msgEl = screen.getByText(/declined by 10 percentage points/);
      expect(msgEl).toBeInTheDocument();
      expect(msgEl.className).toContain('text-red-400');
    });

    it('does not render delta message when only pre_investment is present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ pre_investment: makeWindowStats(0.60), post_investment: null })}
          loading={false}
        />
      );
      expect(screen.queryByText(/percentage points/)).toBeNull();
    });

    it('does not render delta message when only post_investment is present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ pre_investment: null, post_investment: makeWindowStats(0.60) })}
          loading={false}
        />
      );
      expect(screen.queryByText(/percentage points/)).toBeNull();
    });
  });

  describe('recent window cards', () => {
    // Recent window cards render inside the (pre_investment || post_investment) block,
    // so we include pre_investment to activate that block.
    it('shows recent_24h card when present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_24h: makeWindowStats(0.7), pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.getByText('Recent 24h')).toBeInTheDocument();
      expect(screen.getByText('70%')).toBeInTheDocument();
    });

    it('hides recent_24h card when absent', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_24h: null, pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.queryByText('Recent 24h')).toBeNull();
    });

    it('shows -- when recent_24h merge_rate is null', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_24h: makeWindowStats(null), pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.getByText('Recent 24h')).toBeInTheDocument();
      expect(screen.getAllByText('--').length).toBeGreaterThan(0);
    });

    it('shows recent_3d card when present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_3d: makeWindowStats(0.5), pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.getByText('Recent 3d')).toBeInTheDocument();
    });

    it('shows recent_7d card when present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_7d: makeWindowStats(0.5), pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.getByText('Recent 7d')).toBeInTheDocument();
    });

    it('shows recent_30d card when present', () => {
      render(
        <MediumScopeWidget
          data={makeData({ recent_30d: makeWindowStats(0.5), pre_investment: makeWindowStats(0.6) })}
          loading={false}
        />
      );
      expect(screen.getByText('Recent 30d')).toBeInTheDocument();
    });
  });

  describe('daily_series chart', () => {
    it('renders chart when daily_series has >= 2 entries', () => {
      render(
        <MediumScopeWidget data={makeData({ daily_series: makeDailySeries(2) })} loading={false} />
      );
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('does not render chart when daily_series is empty', () => {
      render(
        <MediumScopeWidget data={makeData({ daily_series: [] })} loading={false} />
      );
      expect(screen.queryByTestId('responsive-container')).toBeNull();
    });

    it('does not render chart when daily_series has only 1 entry', () => {
      render(
        <MediumScopeWidget data={makeData({ daily_series: makeDailySeries(1) })} loading={false} />
      );
      expect(screen.queryByTestId('responsive-container')).toBeNull();
    });
  });

  describe('correlated_changes', () => {
    it('renders nothing when correlated_changes is empty', () => {
      render(<MediumScopeWidget data={makeData({ correlated_changes: [] })} loading={false} />);
      expect(screen.queryByText('Correlated Changes')).toBeNull();
    });

    it('truncates to first 5 entries when more than 5', () => {
      const changes: CorrelatedChange[] = Array.from({ length: 7 }, (_, i) => ({
        title: `Change ${i + 1}`,
        week: '2026-01-05',
        merge_rate_delta: 0.1,
      }));
      render(<MediumScopeWidget data={makeData({ correlated_changes: changes })} loading={false} />);
      expect(screen.getByText('Change 1')).toBeInTheDocument();
      expect(screen.getByText('Change 5')).toBeInTheDocument();
      expect(screen.queryByText('Change 6')).toBeNull();
      expect(screen.queryByText('Change 7')).toBeNull();
    });

    it('applies emerald color for positive delta', () => {
      // Use amber overall rate so text-emerald-400 only comes from the delta span
      const changes: CorrelatedChange[] = [{ title: 'Good change', week: '2026-01-05', merge_rate_delta: 0.1 }];
      const { container } = render(
        <MediumScopeWidget
          data={makeData({ medium_scope_conversion_rate: 0.4, correlated_changes: changes })}
          loading={false}
        />
      );
      expect(container.querySelector('.text-emerald-400.font-mono')).not.toBeNull();
    });

    it('applies red color for negative delta', () => {
      // Use emerald overall rate so text-red-400 only comes from the delta span
      const changes: CorrelatedChange[] = [{ title: 'Bad change', week: '2026-01-05', merge_rate_delta: -0.1 }];
      const { container } = render(
        <MediumScopeWidget
          data={makeData({ medium_scope_conversion_rate: 0.7, correlated_changes: changes })}
          loading={false}
        />
      );
      expect(container.querySelector('.text-red-400.font-mono')).not.toBeNull();
    });

    it('shows no pp value when delta is null (neutral)', () => {
      const changes: CorrelatedChange[] = [{ title: 'Unknown change', week: '2026-01-05', merge_rate_delta: null }];
      render(<MediumScopeWidget data={makeData({ correlated_changes: changes })} loading={false} />);
      expect(screen.queryByText(/pp/)).toBeNull();
    });

    it('shows + prefix for positive delta', () => {
      const changes: CorrelatedChange[] = [{ title: 'Good change', week: '2026-01-05', merge_rate_delta: 0.15 }];
      render(<MediumScopeWidget data={makeData({ correlated_changes: changes })} loading={false} />);
      expect(screen.getByText('+15pp')).toBeInTheDocument();
    });

    it('shows negative prefix for negative delta', () => {
      const changes: CorrelatedChange[] = [{ title: 'Bad change', week: '2026-01-05', merge_rate_delta: -0.1 }];
      render(<MediumScopeWidget data={makeData({ correlated_changes: changes })} loading={false} />);
      expect(screen.getByText('-10pp')).toBeInTheDocument();
    });
  });
});
