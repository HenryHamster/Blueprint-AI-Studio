import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('./StrategicAuditPanel', () => ({
  StrategicAuditPanel: () => null,
}));

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }));
vi.mock('../api', () => ({ apiFetch: apiFetchMock }));

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
            correctness: { miss_rate: 0.4, human_count: 5, miss_examples: [{ desc: 'missed a bug', severity: 'HIGH' }] },
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
