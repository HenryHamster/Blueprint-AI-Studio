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

describe('SrTrendBreakdown all_iterations_miss_rate', () => {
  it('shows all-iterations miss rate alongside first-attempt miss rate when present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          all_iterations_miss_rate: 0.35,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    // first-attempt rate
    expect(screen.getByText('20%')).toBeInTheDocument();
    // all-iterations rate
    expect(screen.getByTitle(/all-iterations/i)).toHaveTextContent('all:35%');
  });

  it('does not render all-iterations label when all_iterations_miss_rate is absent (backward compat)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/all-iterations/i)).not.toBeInTheDocument();
  });

  it('SR passes category but iteration-2 flags it: first-attempt shows as hit, all-iterations shows higher miss rate', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,          // first-attempt: mostly caught
          all_iterations_miss_rate: 0.25,  // all-iterations: higher — iter-2 misses counted
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByText('10%')).toBeInTheDocument();
    expect(screen.getByTitle(/all-iterations/)).toHaveTextContent('all:25%');
  });
});

describe('SrTrendBreakdown false_positive_rate and precision', () => {
  it('shows fp rate and precision when present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          false_positive_rate: 0.4,
          precision: 0.6,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTitle(/false-positive rate/i)).toHaveTextContent('fp:40%');
    expect(screen.getByTitle(/precision/i)).toHaveTextContent('prec:60%');
  });

  it('does not render fp/precision labels when absent (backward compat)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/false-positive rate/i)).not.toBeInTheDocument();
    expect(screen.queryByTitle(/precision/i)).not.toBeInTheDocument();
  });

  it('applies alert color (amber) when fp rate exceeds 30%', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,
          false_positive_rate: 0.35,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const fpEl = screen.getByTitle(/false-positive rate/i);
    expect(fpEl).toHaveTextContent('fp:35%');
    expect(fpEl.className).toContain('text-amber-400');
  });

  it('applies alert color (red) when fp rate exceeds 50%', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,
          false_positive_rate: 0.6,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const fpEl = screen.getByTitle(/false-positive rate/i);
    expect(fpEl).toHaveTextContent('fp:60%');
    expect(fpEl.className).toContain('text-red-400');
  });

  it('SR fails category but no reviewer issue: FP rate is non-zero; SR passes: FP unaffected', () => {
    // SR fails correctness with no confirmation → fp rate shows
    // SR fails style with iteration-2 confirmation → shows as hit (low fp rate)
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.0,
          false_positive_rate: 0.8,  // SR said FAILED but nothing confirmed it
          precision: 0.2,
          trend: 'stable',
        },
        'style-conventions': {
          miss_rate: 0.1,
          false_positive_rate: 0.05,  // SR-fails-X and iter-2 confirmed → hit, low FP
          precision: 0.95,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const fpEls = screen.getAllByTitle(/false-positive rate/i);
    expect(fpEls).toHaveLength(2);
    const texts = fpEls.map(el => el.textContent);
    expect(texts).toContain('fp:80%');
    expect(texts).toContain('fp:5%');
  });
});

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

describe('SrTrendBreakdown skip rate line', () => {
  it('renders skip rate line when sr_skip_rate is present', () => {
    const trends: SrTrendData = {
      ...makeTrends(),
      sr_skip_rate: 0.3,
      sr_skip_reason_distribution: { diff_too_small: 2, json_missing: 1 },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-skip-rate-line')).toBeInTheDocument();
    expect(screen.getByTestId('sr-skip-rate-line').textContent).toContain('30%');
    expect(screen.getByTestId('sr-skip-rate-line').textContent).toContain('diff_too_small');
  });

  it('hides skip rate line when sr_skip_rate is null', () => {
    const trends: SrTrendData = { ...makeTrends(), sr_skip_rate: null };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-skip-rate-line')).not.toBeInTheDocument();
  });

  it('hides skip rate line when sr_skip_rate is absent', () => {
    render(<SrTrendBreakdown trends={makeTrends()} variant="card" />);
    expect(screen.queryByTestId('sr-skip-rate-line')).not.toBeInTheDocument();
  });

  it('applies amber styling when skip rate exceeds 25%', () => {
    const trends: SrTrendData = {
      ...makeTrends(),
      sr_skip_rate: 0.4,
      sr_skip_reason_distribution: { json_missing: 2 },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const line = screen.getByTestId('sr-skip-rate-line');
    expect(line.className).toContain('amber');
  });

  it('uses neutral styling when skip rate is below 25%', () => {
    const trends: SrTrendData = {
      ...makeTrends(),
      sr_skip_rate: 0.1,
      sr_skip_reason_distribution: { diff_too_small: 1 },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const line = screen.getByTestId('sr-skip-rate-line');
    expect(line.className).not.toContain('amber');
  });
});
