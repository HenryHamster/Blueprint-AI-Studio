import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CatchUpLatestCycleSection, type CatchUpLatestCycleSectionProps } from './CatchUpLatestCycleSection';

function makeCycle(
  overrides: Partial<CatchUpLatestCycleSectionProps['latestCycle']> = {},
): CatchUpLatestCycleSectionProps['latestCycle'] {
  return {
    cycle_id: 'cycle-1',
    started_at: new Date(Date.now() - 3600000).toISOString(),
    completed_at: new Date().toISOString(),
    outcome: 'completed',
    directions_created: 1,
    directions_retired: 0,
    vision_updated: false,
    ...overrides,
  };
}

describe('CatchUpLatestCycleSection', () => {
  it('renders the cycle outcome and direction deltas', () => {
    render(<CatchUpLatestCycleSection latestCycle={makeCycle()} />);
    expect(screen.getByText('Latest strategic cycle')).toBeInTheDocument();
    expect(screen.getByText('completed')).toBeInTheDocument();
    expect(screen.getByText(/\+1 \/ −0 directions/)).toBeInTheDocument();
  });

  it('renders the strategic challenge chip when the cycle carried a challenge', () => {
    render(
      <CatchUpLatestCycleSection
        latestCycle={makeCycle({
          strategic_judgment_challenge: {
            mode: 'ok',
            verdict: 'warned',
            warned_count: 3,
            blocked_count: 0,
          },
        })}
      />,
    );
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: ok · warned');
    expect(chip).toHaveTextContent('3 warned');
  });

  it('renders no challenge chip for a legacy cycle without a summary', () => {
    render(<CatchUpLatestCycleSection latestCycle={makeCycle()} />);
    expect(screen.queryByLabelText(/strategic judgment challenge/i)).not.toBeInTheDocument();
  });
});
