import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  SrTrendBreakdown,
  DirectionSrBreakdownPanel,
  EdgesTrendBreakdown,
  type SrTrendData,
  type DirectionSrEntry,
  type EdgesTrend,
} from './SrTrendBreakdown';

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

describe('DirectionSrBreakdownPanel', () => {
  it('returns null when entries array is empty', () => {
    const { container } = render(<DirectionSrBreakdownPanel entries={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders entries sorted worst-first (lowest overall catch rate first)', () => {
    const entries: DirectionSrEntry[] = [
      { direction_id: 'd1', direction_text: 'Dir A', sample_size: 10, overall_catch_rate: 0.8, worst_category: null, categories: {} },
      { direction_id: 'd2', direction_text: 'Dir B', sample_size: 10, overall_catch_rate: 0.3, worst_category: null, categories: {} },
      { direction_id: 'd3', direction_text: 'Dir C', sample_size: 10, overall_catch_rate: 0.5, worst_category: null, categories: {} },
    ];
    render(<DirectionSrBreakdownPanel entries={entries} />);
    const rates = screen.getAllByTitle('overall SR catch rate for this direction');
    expect(rates[0].textContent).toBe('30%');
    expect(rates[1].textContent).toBe('50%');
    expect(rates[2].textContent).toBe('80%');
  });

  it('shows category breakdown sorted worst-first within each direction', () => {
    const entries: DirectionSrEntry[] = [{
      direction_id: 'd1',
      direction_text: 'Test Direction',
      sample_size: 5,
      overall_catch_rate: 0.4,
      worst_category: 'correctness',
      categories: {
        style: { human_count: 2, hit_count: 1, miss_rate: 0.5, miss_examples: [] },
        correctness: { human_count: 3, hit_count: 1, miss_rate: 0.67, miss_examples: [] },
      },
    }];
    render(<DirectionSrBreakdownPanel entries={entries} />);
    expect(screen.getByText('67% miss')).toBeInTheDocument();
    expect(screen.getByText('50% miss')).toBeInTheDocument();
  });

  it('renders miss examples via ExampleList', () => {
    const entries: DirectionSrEntry[] = [{
      direction_id: 'd1',
      direction_text: 'Test Direction',
      sample_size: 5,
      overall_catch_rate: 0.4,
      worst_category: 'correctness',
      categories: {
        correctness: {
          human_count: 3, hit_count: 1, miss_rate: 0.67,
          miss_examples: [{ desc: 'null check missed', severity: 'CRITICAL' }],
        },
      },
    }];
    render(<DirectionSrBreakdownPanel entries={entries} />);
    expect(screen.getByText('null check missed')).toBeInTheDocument();
  });

  it('invokes onDirectionClick when direction label is clicked', async () => {
    const onDirectionClick = vi.fn();
    const user = userEvent.setup();
    const entries: DirectionSrEntry[] = [{
      direction_id: 'd1',
      direction_text: 'Clickable Direction',
      sample_size: 5,
      overall_catch_rate: 0.4,
      worst_category: null,
      categories: {},
    }];
    render(<DirectionSrBreakdownPanel entries={entries} onDirectionClick={onDirectionClick} />);
    await user.click(screen.getByText('Clickable Direction'));
    expect(onDirectionClick).toHaveBeenCalledWith('d1');
  });
});

describe('EdgesTrendBreakdown', () => {
  function makeEdgesTrend(overrides: Partial<EdgesTrend> = {}): EdgesTrend {
    return {
      computed_at: '2026-05-01T00:00:00',
      bucket_mode: 'cycle',
      current_cycle_id: 'abcdef123456',
      previous_cycle_id: 'fedcba654321',
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
      },
      previous: {
        sample_size: 8,
        tasks_with_edges_rate: 0.5,
      },
      tasks_with_edges_rate_delta: 0.2,
      trend: 'improving',
      rate_history: [0.3, 0.4, 0.6],
      ...overrides,
    };
  }

  it('renders the edges-trend block when data is present', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.getByTestId('edges-trend-breakdown')).toBeTruthy();
    expect(screen.getByText('Edges considered')).toBeTruthy();
    expect(screen.getByText('70%')).toBeTruthy();
  });

  it('renders null when trend is null', () => {
    const { container } = render(<EdgesTrendBreakdown trend={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders null when trend.current is missing', () => {
    const { container } = render(<EdgesTrendBreakdown trend={{ trend: 'stable' }} />);
    expect(container.firstChild).toBeNull();
  });

  it('shows cycle label when bucket_mode is cycle', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.getByText(/123456 vs 654321/)).toBeTruthy();
  });

  it('shows sample size', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.getByText('n=10')).toBeTruthy();
  });

  it('shows rate history entries', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.getByText('30%')).toBeTruthy();
    expect(screen.getByText('40%')).toBeTruthy();
    expect(screen.getByText('60%')).toBeTruthy();
  });

  it('shows previous rate in footer when available', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    const prevItems = screen.getAllByText('50%');
    expect(prevItems.length).toBeGreaterThanOrEqual(1);
  });

  it('does not show cycle label in calendar mode', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend({ bucket_mode: 'calendar', current_cycle_id: null })} />);
    expect(screen.queryByText(/vs/)).toBeNull();
  });

  it('renders unhandled-flagged and with/without correctness rejection rates when present', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      rate_history: [],
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
        unhandled_edge_flagged_rate: 0.25,
        correctness_rejection_rate_with_edges: 0.10,
        correctness_rejection_rate_without_edges: 0.35,
      },
    })} />);
    expect(screen.getByText('unhandled flagged')).toBeTruthy();
    expect(screen.getByText('25%')).toBeTruthy();
    expect(screen.getByText('correctness rej w/wo')).toBeTruthy();
    expect(screen.getByText('10%')).toBeTruthy();
    expect(screen.getByText('35%')).toBeTruthy();
  });

  it('shows positive gap when without-edges rejection is higher (edges helping)', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_edges: 0.10,
        correctness_rejection_rate_without_edges: 0.30,
      },
    })} />);
    const gap = container.querySelector('[title*="positive = edges correlate"]');
    expect(gap).not.toBeNull();
    expect(gap?.textContent).toBe('Δ+20pp');
    expect(gap?.className).toContain('text-emerald-400');
  });

  it('shows negative gap colored red when with-edges rejection is higher', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_edges: 0.40,
        correctness_rejection_rate_without_edges: 0.20,
      },
    })} />);
    const gap = container.querySelector('[title*="positive = edges correlate"]');
    expect(gap).not.toBeNull();
    expect(gap?.textContent).toBe('Δ−20pp');
    expect(gap?.className).toContain('text-red-400');
  });

  it('renders without crashing and without new rows when extended fields are absent', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.getByTestId('edges-trend-breakdown')).toBeTruthy();
    expect(screen.queryByText('unhandled flagged')).toBeNull();
    expect(screen.queryByText('correctness rej w/wo')).toBeNull();
  });

  it('omits the gap when only one side of correctness rejection is present', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_edges: 0.15,
      },
    })} />);
    expect(screen.getByText('correctness rej w/wo')).toBeTruthy();
    expect(container.querySelector('[title*="positive = edges correlate"]')).toBeNull();
  });
});
