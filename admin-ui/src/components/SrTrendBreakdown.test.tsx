import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SrTrendBreakdown, type SrTrendData } from './SrTrendBreakdown';

function makeTrends(): SrTrendData {
  return {
    categories: {
      correctness: {
        current_hit_rate: 0.7,
        previous_hit_rate: 0.8,
        miss_rate: 0.3,
        trend: 'decreasing',
        examples: [
          { severity: 'CRITICAL', desc: 'missed null check on payload field' },
          { severity: 'MAJOR', desc: 'off-by-one in pagination loop' },
        ],
      },
    },
  };
}

describe('SrTrendBreakdown CategoryRow chevron', () => {
  it('clicking the chevron expands examples without invoking onCategoryClick', async () => {
    const onCategoryClick = vi.fn();
    const user = userEvent.setup();

    render(
      <SrTrendBreakdown trends={makeTrends()} variant="card" onCategoryClick={onCategoryClick} />,
    );

    expect(screen.queryByText(/missed null check on payload field/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /expand correctness examples/i }));

    expect(screen.getByText(/missed null check on payload field/)).toBeInTheDocument();
    expect(screen.getByText(/off-by-one in pagination loop/)).toBeInTheDocument();
    expect(onCategoryClick).not.toHaveBeenCalled();
  });

  it('keyboard activation of the chevron expands examples without drilling into the category', async () => {
    const onCategoryClick = vi.fn();
    const user = userEvent.setup();

    render(
      <SrTrendBreakdown trends={makeTrends()} variant="card" onCategoryClick={onCategoryClick} />,
    );

    const chevron = screen.getByRole('button', { name: /expand correctness examples/i });
    chevron.focus();
    await user.keyboard('{Enter}');

    expect(screen.getByText(/missed null check on payload field/)).toBeInTheDocument();
    expect(onCategoryClick).not.toHaveBeenCalled();
  });

  it('clicking the row body invokes onCategoryClick (chevron is a separate target)', async () => {
    const onCategoryClick = vi.fn();
    const user = userEvent.setup();

    render(
      <SrTrendBreakdown trends={makeTrends()} variant="card" onCategoryClick={onCategoryClick} />,
    );

    await user.click(screen.getByText('correctness'));
    expect(onCategoryClick).toHaveBeenCalledWith('correctness');
    expect(screen.queryByText(/missed null check on payload field/)).not.toBeInTheDocument();
  });

  it('toggles examples closed on second chevron click', async () => {
    const user = userEvent.setup();
    render(<SrTrendBreakdown trends={makeTrends()} variant="card" />);

    const chevron = screen.getByRole('button', { name: /expand correctness examples/i });
    await user.click(chevron);
    expect(screen.getByText(/missed null check on payload field/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /collapse correctness examples/i }));
    expect(screen.queryByText(/missed null check on payload field/)).not.toBeInTheDocument();
  });
});
