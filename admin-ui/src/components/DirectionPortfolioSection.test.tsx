import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('./StrategicAuditPanel', () => ({
  StrategicAuditPanel: () => null,
}));

const { apiFetchMock, fetchEvictedFindingsMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  fetchEvictedFindingsMock: vi.fn(),
}));
vi.mock('../api', () => ({
  apiFetch: apiFetchMock,
  fetchEvictedFindings: fetchEvictedFindingsMock,
  API_BASE_URL: '',
  authGet: vi.fn(),
}));

import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';

function makeDirection(overrides: Partial<DirectionProgress> = {}): DirectionProgress {
  return {
    direction_id: 'dir-1',
    direction_text: 'Improve retry handling',
    priority: 'high',
    cycles_in_window: 2,
    tasks_created: 3,
    prs_merged: 2,
    possibly_saturated: false,
    ...overrides,
  };
}

describe('DirectionPortfolioSection', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({ ok: false });
    fetchEvictedFindingsMock.mockReset();
  });
  it('shows loading skeleton state', () => {
    const { container } = render(
      <DirectionPortfolioSection
        directions={[]}
        loading
        productPct={null}
        totalDirPRs={0}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={0}
      />,
    );

    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('shows empty state when no directions and not loading', () => {
    render(
      <DirectionPortfolioSection
        directions={[]}
        loading={false}
        productPct={null}
        totalDirPRs={0}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={0}
      />,
    );

    expect(screen.getByText('No active directions')).toBeInTheDocument();
  });

  it('renders direction cards with priority badge and direction text', () => {
    render(
      <DirectionPortfolioSection
        directions={[
          makeDirection(),
          makeDirection({ direction_id: 'dir-2', direction_text: 'Reduce flake retries', priority: 'medium' }),
        ]}
        loading={false}
        productPct={40}
        totalDirPRs={3}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
      />,
    );

    expect(screen.getByText('Improve retry handling')).toBeInTheDocument();
    expect(screen.getByText('Reduce flake retries')).toBeInTheDocument();
    expect(screen.getByText('high')).toBeInTheDocument();
    expect(screen.getByText('medium')).toBeInTheDocument();
  });

  it('shows merged total and autonomous/assisted split when attribution exists', () => {
    render(
      <DirectionPortfolioSection
        directions={[makeDirection()]}
        loading={false}
        productPct={null}
        totalDirPRs={7}
        hasAttribution
        totalAutonomousPRs={5}
        totalAssistedPRs={2}
        selfSufficientCount={1}
      />,
    );

    expect(screen.getByText('7 PRs merged')).toBeInTheDocument();
    expect(screen.getByText('5 autonomous')).toBeInTheDocument();
    expect(screen.getByText('2 assisted')).toBeInTheDocument();
  });

  it('invokes onDirectionClick with the clicked direction id', async () => {
    const onDirectionClick = vi.fn();
    const user = userEvent.setup();

    render(
      <DirectionPortfolioSection
        directions={[makeDirection({ direction_id: 'dir-click', direction_text: 'Click me direction' })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
        onDirectionClick={onDirectionClick}
      />,
    );

    await user.click(screen.getByText('Click me direction'));
    expect(onDirectionClick).toHaveBeenCalledWith('dir-click');
  });

  it('surfaces generalized eval-success alongside PR metrics when non-code lanes exist', () => {
    render(
      <DirectionPortfolioSection
        directions={[makeDirection({
          generalized_successes: 5,
          generalized_terminal_outcomes: 8,
          generalized_health_rate: 0.625,
          outcome_kind_breakdown: {
            code_merge: { success: 2, neutral: 0, failure: 1 },
            research_quality: { success: 3, neutral: 1, failure: 0 },
          },
        })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
      />,
    );

    // PR metric still present (not hidden).
    expect(screen.getByText('2 PRs merged')).toBeInTheDocument();
    // Summary eval-success metric.
    expect(screen.getByText('5 eval-success · 63%')).toBeInTheDocument();
    // Per-card inline eval badge.
    expect(screen.getByText('5 eval · 63%')).toBeInTheDocument();
    // Per-kind breakdown chips.
    expect(screen.getByText('research 3/4')).toBeInTheDocument();
    expect(screen.getByText('code 2/3')).toBeInTheDocument();
  });

  it('hides generalized metrics for legacy data with only code_merge outcomes', () => {
    render(
      <DirectionPortfolioSection
        directions={[makeDirection({
          generalized_successes: 2,
          generalized_terminal_outcomes: 2,
          generalized_health_rate: 1,
          outcome_kind_breakdown: { code_merge: { success: 2, neutral: 0, failure: 0 } },
        })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
      />,
    );

    expect(screen.getByText('2 PRs merged')).toBeInTheDocument();
    expect(screen.queryByText(/eval-success/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\beval\b/)).not.toBeInTheDocument();
  });

  it('renders SR catch-rate pill when direction-sr-breakdown returns data', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        directions: [{
          direction_id: 'dir-1',
          sample_size: 10,
          overall_catch_rate: 0.6,
          worst_category: 'correctness',
          categories: {
            correctness: { miss_rate: 0.4, sample_count: 5, examples: [{ desc: 'missed a bug', severity: 'HIGH' }] },
          },
        }],
      }),
    });

    await act(async () => {
      render(
        <DirectionPortfolioSection
          directions={[makeDirection({ direction_id: 'dir-1' })]}
          loading={false}
          productPct={null}
          totalDirPRs={2}
          hasAttribution={false}
          totalAutonomousPRs={null}
          totalAssistedPRs={null}
          selfSufficientCount={1}
          projectId="proj-1"
        />,
      );
    });

    expect(screen.getByText(/SR: 60% catch/)).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/projects/proj-1/direction-sr-breakdown');
  });

  it('renders trend arrow and delta when direction-sr-breakdown includes trend data', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        directions: [{
          direction_id: 'dir-1',
          sample_size: 12,
          overall_catch_rate: 0.65,
          worst_category: null,
          categories: {},
          trend: 'increasing',
          previous_miss_rate: 0.25,
          rate_history: [0.85, 0.75, 0.65],
        }],
      }),
    });

    await act(async () => {
      render(
        <DirectionPortfolioSection
          directions={[makeDirection({ direction_id: 'dir-1' })]}
          loading={false}
          productPct={null}
          totalDirPRs={2}
          hasAttribution={false}
          totalAutonomousPRs={null}
          totalAssistedPRs={null}
          selfSufficientCount={1}
          projectId="proj-1"
        />,
      );
    });

    expect(screen.getByText(/SR: 65% catch/)).toBeInTheDocument();
    // Backend trend = 'increasing' means miss-rate went UP (0.25 → 0.35), i.e. catch-rate DOWN (75% → 65%) — a regression.
    // UI must render red ↓ with negative pp delta (65% - 75% = -10pp).
    const trendEl = screen.getByText(/↓-10pp/);
    expect(trendEl).toBeInTheDocument();
    expect(trendEl).toHaveClass('text-red-400');
  });

  it('shows an evicted findings badge only when the aggregate count is positive', () => {
    render(
      <DirectionPortfolioSection
        directions={[
          makeDirection({ direction_id: 'dir-1', history_aggregate: { evicted_findings: 3 } }),
          makeDirection({ direction_id: 'dir-2', direction_text: 'No evictions', history_aggregate: { evicted_findings: 0 } }),
        ]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
      />,
    );

    expect(screen.getByRole('button', { name: /view evicted findings for this direction/i })).toHaveTextContent('3 evicted');
    expect(screen.queryByText('0 evicted')).not.toBeInTheDocument();
  });

  it('opens the evicted findings drawer and paginates results', async () => {
    const user = userEvent.setup();
    const pageOne = Array.from({ length: 10 }, (_, i) => ({
      title: `Evicted finding ${i + 1}`,
      claims: [`Claim ${i + 1}`],
      source_refs: [`backend/app/example_${i + 1}.py:12`],
      status: 'evicted',
      evicted_at: new Date(Date.now() - (i + 1) * 60_000).toISOString(),
    }));
    const pageTwo = [{
      title: 'Evicted finding 11',
      claims: ['Claim 11'],
      source_refs: ['https://example.com/finding/11'],
      status: 'evicted',
      evicted_at: new Date(Date.now() - 11 * 60_000).toISOString(),
    }];

    fetchEvictedFindingsMock.mockImplementation(async (_projectId: string, _directionId: string, params?: { offset?: number; limit?: number }) => {
      if (params?.offset === 10) {
        return { direction_id: 'dir-1', total: 11, offset: 10, limit: 10, evicted_findings: pageTwo };
      }
      return { direction_id: 'dir-1', total: 11, offset: 0, limit: 10, evicted_findings: pageOne };
    });

    render(
      <DirectionPortfolioSection
        directions={[makeDirection({ direction_id: 'dir-1', direction_text: 'Eviction demo', history_aggregate: { evicted_findings: 11 } })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
        projectId="proj-1"
      />,
    );

    await user.click(screen.getByRole('button', { name: /view evicted findings for this direction/i }));

    expect(await screen.findByText('Evicted finding 1')).toBeInTheDocument();
    expect(screen.getByText('Claim 1')).toBeInTheDocument();
    expect(screen.getByText('backend/app/example_1.py:12')).toBeInTheDocument();
    expect(fetchEvictedFindingsMock).toHaveBeenCalledWith('proj-1', 'dir-1', { offset: 0, limit: 10 });

    await user.click(screen.getByRole('button', { name: /next page/i }));

    expect(await screen.findByText('Evicted finding 11')).toBeInTheDocument();
    expect(fetchEvictedFindingsMock).toHaveBeenCalledWith('proj-1', 'dir-1', { offset: 10, limit: 10 });
  });

  it('renders empty and error drawer states', async () => {
    const user = userEvent.setup();
    fetchEvictedFindingsMock.mockResolvedValueOnce({
      direction_id: 'dir-1',
      total: 0,
      offset: 0,
      limit: 10,
      evicted_findings: [],
    });

    const { rerender } = render(
      <DirectionPortfolioSection
        directions={[makeDirection({ direction_id: 'dir-1', direction_text: 'Empty evictions', history_aggregate: { evicted_findings: 1 } })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
        projectId="proj-1"
      />,
    );

    await user.click(screen.getByRole('button', { name: /view evicted findings for this direction/i }));
    expect(await screen.findByText('No evicted findings for this direction')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /close evicted findings drawer/i }));
    fetchEvictedFindingsMock.mockRejectedValueOnce(new Error('boom'));
    rerender(
      <DirectionPortfolioSection
        directions={[makeDirection({ direction_id: 'dir-1', direction_text: 'Empty evictions', history_aggregate: { evicted_findings: 1 } })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
        projectId="proj-1"
      />,
    );

    await user.click(screen.getByRole('button', { name: /view evicted findings for this direction/i }));
    expect(await screen.findByText('Error fetching evicted findings')).toBeInTheDocument();
  });

  it('does not render SR pill when no project id is provided', () => {
    render(
      <DirectionPortfolioSection
        directions={[makeDirection({ direction_id: 'dir-1' })]}
        loading={false}
        productPct={null}
        totalDirPRs={2}
        hasAttribution={false}
        totalAutonomousPRs={null}
        totalAssistedPRs={null}
        selfSufficientCount={1}
      />,
    );

    expect(screen.queryByText(/SR:/)).not.toBeInTheDocument();
    expect(apiFetchMock).not.toHaveBeenCalled();
  });
});
