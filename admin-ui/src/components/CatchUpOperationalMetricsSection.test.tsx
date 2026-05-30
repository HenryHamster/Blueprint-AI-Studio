import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { CatchUpOperationalMetricsSection } from './CatchUpOperationalMetricsSection';

describe('CatchUpOperationalMetricsSection', () => {
  it('renders nothing when no metric data provided', () => {
    const { container } = render(
      <CatchUpOperationalMetricsSection directionProgress={[]} />
    );
    expect(container.textContent).toBe('');
  });

  it('renders the review cap exhaustion strip when data present', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        reviewCapExhaustionSummary={{
          count: 3,
          avg_iterations: 4.2,
          total_cost_usd: 12.5,
          avg_cost_usd: 4.17,
          top_directions: [{ direction_id: 'd-1', count: 2 }],
        }}
      />
    );
    expect(screen.getByText('Review cap exhaustion')).toBeInTheDocument();
    expect(screen.getByText(/3 tasks hit cap/)).toBeInTheDocument();
    expect(screen.getByText(/\$12\.50 wasted/)).toBeInTheDocument();
  });

  it('renders the early-exit strip with correct singular/plural', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        totalMergeRetriesSaved={1}
        mergeEarlyExitCount={1}
      />
    );
    expect(screen.getByText('Early exit')).toBeInTheDocument();
    expect(screen.getByText(/1 retry saved/)).toBeInTheDocument();
    expect(screen.getByText(/across 1 task/)).toBeInTheDocument();
  });

  it('renders the retry success by class strip with computed percentage', () => {
    render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        retrySuccessRateByClass={{
          lint_error: { merged: 3, failed: 1, success_rate: 0.75 },
        }}
      />
    );
    expect(screen.getByText('Retry success by class')).toBeInTheDocument();
    expect(screen.getByText(/\(75%\)/)).toBeInTheDocument();
  });

  it('renders the standalone provisioning overhead strip only when no provision waste', () => {
    const { rerender } = render(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        provisioningOverheadPct={0.18}
      />
    );
    expect(screen.getByText('Provisioning overhead')).toBeInTheDocument();

    rerender(
      <CatchUpOperationalMetricsSection
        directionProgress={[]}
        provisionWasteCostUsd={5}
        provisioningOverheadPct={0.18}
      />
    );
    // Standalone strip suppressed; provision-waste strip shows the percentage inline instead
    expect(screen.getByText('Provision waste')).toBeInTheDocument();
    expect(screen.queryByText('Provisioning overhead')).not.toBeInTheDocument();
  });
});
