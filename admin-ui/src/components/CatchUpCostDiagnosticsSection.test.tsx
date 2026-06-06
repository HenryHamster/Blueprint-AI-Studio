import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { CatchUpCostDiagnosticsSection } from './CatchUpCostDiagnosticsSection';

vi.mock('./catchupAnalytics', () => ({
  trendFor: () => ({ arrow: '', color: '' }),
  sortDesc: (arr: unknown[]) => arr,
}));

const noop = () => {};

describe('CatchUpCostDiagnosticsSection', () => {
  it('renders nothing when no data provided', () => {
    const { container } = render(
      <CatchUpCostDiagnosticsSection
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(container.textContent).toBe('');
  });

  it('renders taxonomy pills when failure taxonomy data present', () => {
    render(
      <CatchUpCostDiagnosticsSection
        failureTaxonomyDistribution={{
          categories: { scope_related: { count: 3, percentage: 60 } },
          total_failures: 5,
          dominant_category: null,
        }}
        failureGroups={[]}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(screen.getByText('Root cause')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /scope/i })).toBeInTheDocument();
  });

  it('calls onTaxonomyFilterChange and onExpandFailures when taxonomy pill clicked', () => {
    const onTaxonomyFilterChange = vi.fn();
    const onExpandFailures = vi.fn();
    render(
      <CatchUpCostDiagnosticsSection
        failureTaxonomyDistribution={{
          categories: { scope_related: { count: 2, percentage: 100 } },
          total_failures: 2,
          dominant_category: null,
        }}
        failureGroups={[]}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={onTaxonomyFilterChange}
        onStageFilterChange={noop}
        onExpandFailures={onExpandFailures}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /scope/i }));
    expect(onTaxonomyFilterChange).toHaveBeenCalledWith('scope_related');
    expect(onExpandFailures).toHaveBeenCalled();
  });

  it('renders stage pills when failure stage data present', () => {
    render(
      <CatchUpCostDiagnosticsSection
        failureStageDistribution={{
          stages: { lint: 4 },
          total_classified: 4,
          dominant_stage: 'lint',
        }}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(screen.getByText('Pipeline stage')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /lint/i })).toBeInTheDocument();
  });

  it('calls onStageFilterChange when stage pill clicked', () => {
    const onStageFilterChange = vi.fn();
    render(
      <CatchUpCostDiagnosticsSection
        failureStageDistribution={{
          stages: { lint: 2 },
          total_classified: 2,
          dominant_stage: null,
        }}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={onStageFilterChange}
        onExpandFailures={noop}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /lint/i }));
    expect(onStageFilterChange).toHaveBeenCalledWith('lint');
  });

  it('renders scope conversion rate pills', () => {
    render(
      <CatchUpCostDiagnosticsSection
        scopeConversionRates={{ small: { created: 10, merged: 7 } }}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(screen.getByText('Scope conversion')).toBeInTheDocument();
    expect(screen.getByText(/small/)).toBeInTheDocument();
  });

  it('renders rejection pattern summary', () => {
    const onExpandFailures = vi.fn();
    render(
      <CatchUpCostDiagnosticsSection
        rejectionPatternSummary={{
          total_rejected: 3,
          top_notes: [{ snippet: 'too broad', count: 2 }],
        }}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={onExpandFailures}
      />
    );
    expect(screen.getByText('Rejection patterns')).toBeInTheDocument();
    expect(screen.getByText(/too broad/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /rejected/i }));
    expect(onExpandFailures).toHaveBeenCalled();
  });

  it('renders per-type cost-per-merge table and flags exceeds_threshold', () => {
    render(
      <CatchUpCostDiagnosticsSection
        costPerMergeByType={{
          fix: { total_cost_usd: 12, merged_count: 4, cost_per_merge: 3, exceeds_threshold: false },
          research: { total_cost_usd: 40, merged_count: 2, cost_per_merge: 20, exceeds_threshold: true },
        }}
        medianCostPerMergeByType={3}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(screen.getByText('Cost per merge by type')).toBeInTheDocument();
    expect(screen.getByText(/median \$3\.00/)).toBeInTheDocument();
    // Both task types are listed, sorted by cost-per-merge descending.
    expect(screen.getByText('fix')).toBeInTheDocument();
    expect(screen.getByText('research')).toBeInTheDocument();
    // The above-threshold type carries the warning marker.
    expect(screen.getByLabelText('exceeds threshold')).toBeInTheDocument();
  });

  it('omits cost-per-merge types with no merges (null cost_per_merge)', () => {
    const { container } = render(
      <CatchUpCostDiagnosticsSection
        costPerMergeByType={{
          research: { total_cost_usd: 40, merged_count: 0, cost_per_merge: null, exceeds_threshold: false },
        }}
        activeTaxonomyFilter={null}
        activeStageFilter={null}
        onTaxonomyFilterChange={noop}
        onStageFilterChange={noop}
        onExpandFailures={noop}
      />
    );
    expect(container.textContent).toBe('');
  });
});
