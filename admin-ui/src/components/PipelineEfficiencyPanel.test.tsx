import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PipelineEfficiencyPanel } from './PipelineEfficiencyPanel';

const baseWindow = {
  setup_p50_secs: null,
  setup_p95_secs: null,
  setup_sample_n: 0,
  warm_reuse_rate: 0.6,
  warm_reuse_sample_n: 10,
  review_iter_median: null,
  review_iter_p95: null,
  review_iter_sample_n: 0,
  vs_prior_setup_p50_delta: null,
  vs_prior_warm_reuse_rate_delta: null,
  vs_prior_review_iter_median_delta: null,
};

const snapshotWithBreakdown = {
  computed_at: new Date().toISOString(),
  window_24h: {
    ...baseWindow,
    warm_start_origin_breakdown: {
      warm_pool: 4,
      backlog_reuse: 1,
      chain_continuity: 5,
      warm_total: 10,
      pool_origin_share: 0.4,
    },
  },
  window_7d: { ...baseWindow },
};

const snapshotWithoutBreakdown = {
  computed_at: new Date().toISOString(),
  window_24h: { ...baseWindow },
  window_7d: { ...baseWindow },
};

vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: vi.fn(),
  fmtEffSecs: (s: number | null | undefined) => (s == null ? '—' : `${s}s`),
  fmtEffRate: (r: number | null | undefined) => (r == null ? '—' : `${Math.round(r * 100)}%`),
  fmtEffIter: (n: number | null | undefined) => (n == null ? '—' : n.toFixed(1)),
}));

import { useEfficiencySnapshot } from '../hooks/useEfficiencySnapshot';

describe('PipelineEfficiencyPanel', () => {
  it('renders nothing when snapshot is null', () => {
    (useEfficiencySnapshot as ReturnType<typeof vi.fn>).mockReturnValue(null);
    const { container } = render(<PipelineEfficiencyPanel projectId={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders origin breakdown row when warm_start_origin_breakdown is present', () => {
    (useEfficiencySnapshot as ReturnType<typeof vi.fn>).mockReturnValue(snapshotWithBreakdown);
    render(<PipelineEfficiencyPanel projectId={null} />);
    const row = screen.getByTestId('warm-start-origin-breakdown');
    expect(row.textContent).toContain('warm by origin');
    expect(row.textContent).toContain('4');
    expect(row.textContent).toContain('1');
    expect(row.textContent).toContain('5');
    expect(screen.getByTestId('warm-start-pool-share').textContent).toContain('40%');
  });

  it('does not render breakdown row when warm_start_origin_breakdown is absent', () => {
    (useEfficiencySnapshot as ReturnType<typeof vi.fn>).mockReturnValue(snapshotWithoutBreakdown);
    render(<PipelineEfficiencyPanel projectId={null} />);
    expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
  });

  it('does not render breakdown row when warm_start_origin_breakdown is null', () => {
    (useEfficiencySnapshot as ReturnType<typeof vi.fn>).mockReturnValue({
      ...snapshotWithoutBreakdown,
      window_24h: { ...baseWindow, warm_start_origin_breakdown: null },
    });
    render(<PipelineEfficiencyPanel projectId={null} />);
    expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
  });
});
