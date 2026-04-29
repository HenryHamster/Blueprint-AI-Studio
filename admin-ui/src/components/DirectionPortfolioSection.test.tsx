import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
});
