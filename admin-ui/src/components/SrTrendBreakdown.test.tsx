import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  SrTrendBreakdown,
  SrCatchRateFreshnessPill,
  SrMissedRetrospective,
  SrUnhandledEdgesRetrospective,
  DirectionSrBreakdownPanel,
  EdgesTrendBreakdown,
  MissRateSparkline,
  computeTargetHealth,
  type SrTrendData,
  type DirectionSrEntry,
  type EdgesTrend,
  type SrUnhandledEdgesTask,
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
          current_finding_count: 10,
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
          current_finding_count: 10,
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

describe('SrMissedRetrospective drilldown', () => {
  it('expands to show self-review evidence and keeps the PR link from triggering row click', async () => {
    const user = userEvent.setup();
    const onTaskClick = vi.fn();
    render(
      <SrMissedRetrospective
        tasks={[
          {
            task_id: 't1',
            title: 'Task with self-review evidence',
            task_type: 'create',
            scope: 'small',
            created_at: new Date().toISOString(),
            missed_categories: ['correctness'],
            caught_categories: [],
            pr_url: 'https://example.com/pr/1',
            sr_check_results: { correctness: { passed: true, issue: 'sr note', origin: 'author' } },
          },
        ]}
        onTaskClick={onTaskClick}
      />,
    );

    await user.click(screen.getByRole('button', { name: /expand task with self-review evidence/i }));
    expect(screen.getByText('Self-review evidence')).toBeInTheDocument();
    expect(screen.getByText('origin: author')).toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: 'PR' }));
    expect(onTaskClick).not.toHaveBeenCalled();
  });

  it('falls back to the rejection category label when self-review evidence is missing', async () => {
    const user = userEvent.setup();
    render(
      <SrMissedRetrospective
        tasks={[
          {
            task_id: 't2',
            title: 'Task without self-review evidence',
            created_at: new Date().toISOString(),
            missed_categories: ['correctness'],
            caught_categories: [],
          },
        ]}
      />,
    );

    await user.click(screen.getByRole('button', { name: /expand task without self-review evidence/i }));
    expect(screen.getAllByText('correctness').length).toBeGreaterThanOrEqual(2);
  });

  it('shows self-review origin tags for missed categories', async () => {
    const user = userEvent.setup();
    render(
      <SrMissedRetrospective
        tasks={[
          {
            task_id: 't3',
            title: 'Task with recorded origins',
            created_at: new Date().toISOString(),
            missed_categories: ['correctness', 'performance'],
            caught_categories: [],
            sr_check_results: {
              correctness: { passed: true, origin: 'author' },
              performance: { passed: true, origin: 'author' },
            },
          },
        ]}
      />,
    );

    await user.click(screen.getByRole('button', { name: /expand task with recorded origins/i }));
    expect(screen.getAllByText('origin: author')).toHaveLength(2);
  });

  it('re-syncs the visible list when selectedCategory prop changes', () => {
    const tasks = [
      {
        task_id: 'c1',
        title: 'Correctness task',
        created_at: new Date().toISOString(),
        missed_categories: ['correctness'],
        caught_categories: [],
      },
      {
        task_id: 's1',
        title: 'Security task',
        created_at: new Date().toISOString(),
        missed_categories: ['security'],
        caught_categories: [],
      },
    ];

    const { rerender } = render(<SrMissedRetrospective tasks={tasks} selectedCategory="correctness" />);
    expect(screen.getByText('Correctness task')).toBeInTheDocument();
    expect(screen.queryByText('Security task')).not.toBeInTheDocument();

    rerender(<SrMissedRetrospective tasks={tasks} selectedCategory="security" />);
    expect(screen.queryByText('Correctness task')).not.toBeInTheDocument();
    expect(screen.getByText('Security task')).toBeInTheDocument();
  });
});

describe('SrUnhandledEdgesRetrospective', () => {
  function makeUnhandledTasks(): SrUnhandledEdgesTask[] {
    return [
      {
        task_id: 'u1',
        title: 'Recent create task',
        task_type: 'create',
        scope: 'small',
        created_at: new Date().toISOString(),
        unhandled_edges: [
          { case: 'missing auth branch', handled: false, blind_spot: 'did not inspect role branch', origin: 'author' },
        ],
        pr_url: 'https://example.com/pr/9',
      },
      {
        task_id: 'u2',
        title: 'Recent fix task',
        task_type: 'fix',
        created_at: new Date().toISOString(),
        unhandled_edges: [
          { case: 'legacy rollback path', handled: false },
        ],
      },
    ];
  }

  it('filters by task type and expands edge details', async () => {
    const user = userEvent.setup();
    render(<SrUnhandledEdgesRetrospective tasks={makeUnhandledTasks()} />);

    expect(screen.getByText('Recent create task')).toBeInTheDocument();
    expect(screen.getByText('Recent fix task')).toBeInTheDocument();

    await user.selectOptions(screen.getByDisplayValue('All types'), 'create');
    expect(screen.getByText('Recent create task')).toBeInTheDocument();
    expect(screen.queryByText('Recent fix task')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /expand recent create task/i }));
    expect(screen.getByText('missing auth branch')).toBeInTheDocument();
    expect(screen.getByText('did not inspect role branch')).toBeInTheDocument();
    expect(screen.getByText('source: author')).toBeInTheDocument();
  });

  it('applies the rolling time window when the selected window changes', async () => {
    const user = userEvent.setup();
    const oldDate = new Date(Date.now() - 10 * 86400_000).toISOString();
    const tasks: SrUnhandledEdgesTask[] = [
      {
        task_id: 'recent',
        title: 'Recent task',
        task_type: 'create',
        created_at: new Date().toISOString(),
        unhandled_edges: [{ case: 'recent edge', handled: false }],
      },
      {
        task_id: 'old',
        title: 'Old task',
        task_type: 'create',
        created_at: oldDate,
        unhandled_edges: [{ case: 'old edge', handled: false }],
      },
    ];

    const onWindowDaysChange = vi.fn();
    const { rerender } = render(
      <SrUnhandledEdgesRetrospective
        tasks={tasks}
        windowDays={14}
        onWindowDaysChange={onWindowDaysChange}
      />,
    );
    expect(screen.getByText('Recent task')).toBeInTheDocument();
    expect(screen.getByText('Old task')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '7d' }));
    expect(onWindowDaysChange).toHaveBeenCalledWith(7);
    rerender(
      <SrUnhandledEdgesRetrospective
        tasks={tasks}
        windowDays={7}
        onWindowDaysChange={onWindowDaysChange}
      />,
    );
    expect(screen.getByText('Recent task')).toBeInTheDocument();
    expect(screen.queryByText('Old task')).not.toBeInTheDocument();
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
        style: { sample_count: 6, hit_rate: 0.5, miss_rate: 0.5 },
        correctness: { sample_count: 6, hit_rate: 0.33, miss_rate: 0.67 },
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
          sample_count: 3, hit_rate: 0.33, miss_rate: 0.67,
          examples: [{ desc: 'null check missed', severity: 'CRITICAL' }],
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

  const freshnessEntries: DirectionSrEntry[] = [
    { direction_id: 'd1', direction_text: 'Dir A', sample_size: 10, overall_catch_rate: 0.5, worst_category: null, categories: {} },
  ];

  it('renders the stale badge inside the panel when trends.is_stale is true', () => {
    render(
      <DirectionSrBreakdownPanel
        entries={freshnessEntries}
        trends={{ is_stale: true, computed_at: '2026-05-01T00:00:00' }}
      />,
    );
    const panel = screen.getByTestId('direction-sr-breakdown-panel');
    const badge = within(panel).getByTestId('sr-trend-stale-badge');
    expect(badge.textContent).toBe(`stale · as of ${new Date('2026-05-01T00:00:00').toLocaleDateString()}`);
    expect(within(panel).queryByTestId('sr-trend-insufficient-badge')).toBeNull();
  });

  it('renders the frozen badge with last-real-data date when data_sufficient is false', () => {
    render(
      <DirectionSrBreakdownPanel
        entries={freshnessEntries}
        trends={{ data_sufficient: false, last_sufficient_computed_at: '2026-04-15T00:00:00', computed_at: '2026-05-01T00:00:00' }}
      />,
    );
    const panel = screen.getByTestId('direction-sr-breakdown-panel');
    const badge = within(panel).getByTestId('sr-trend-insufficient-badge');
    expect(badge.textContent).toBe(`frozen · last real data ${new Date('2026-04-15T00:00:00').toLocaleDateString()}`);
  });

  it('falls back to computed_at for the frozen badge when last_sufficient_computed_at is absent', () => {
    render(
      <DirectionSrBreakdownPanel
        entries={freshnessEntries}
        trends={{ data_sufficient: false, computed_at: '2026-05-01T00:00:00' }}
      />,
    );
    const panel = screen.getByTestId('direction-sr-breakdown-panel');
    const badge = within(panel).getByTestId('sr-trend-insufficient-badge');
    expect(badge.textContent).toBe(`frozen · last real data ${new Date('2026-05-01T00:00:00').toLocaleDateString()}`);
  });

  it('renders no freshness badge when trends prop is absent (backward compat)', () => {
    render(<DirectionSrBreakdownPanel entries={freshnessEntries} />);
    const panel = screen.getByTestId('direction-sr-breakdown-panel');
    expect(within(panel).queryByTestId('sr-trend-stale-badge')).toBeNull();
    expect(within(panel).queryByTestId('sr-trend-insufficient-badge')).toBeNull();
  });

  it('renders no freshness badge when snapshot is fresh and sufficient', () => {
    render(
      <DirectionSrBreakdownPanel
        entries={freshnessEntries}
        trends={{ is_stale: false, data_sufficient: true, computed_at: '2026-05-01T00:00:00' }}
      />,
    );
    const panel = screen.getByTestId('direction-sr-breakdown-panel');
    expect(within(panel).queryByTestId('sr-trend-stale-badge')).toBeNull();
    expect(within(panel).queryByTestId('sr-trend-insufficient-badge')).toBeNull();
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

  it('renders rej w/unhandled-flag vs all-handled row when both sub-bucket fields are present', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      rate_history: [],
      current: {
        sample_size: 20,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_unhandled_flag: 0.40,
        correctness_rejection_rate_with_all_handled: 0.15,
      },
    })} />);
    expect(screen.getByText('rej w/unhandled-flag vs all-handled')).toBeTruthy();
    expect(screen.getByText('40%')).toBeTruthy();
    expect(screen.getByText('15%')).toBeTruthy();
  });

  it('shows positive gap colored emerald when unhandled-flag rejection is higher (informative signal)', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 20,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_unhandled_flag: 0.45,
        correctness_rejection_rate_with_all_handled: 0.10,
      },
    })} />);
    const gap = container.querySelector('[title*="positive = author\'s unhandled-flag signal is informative"]');
    expect(gap).not.toBeNull();
    expect(gap?.textContent).toBe('Δ+35pp');
    expect(gap?.className).toContain('text-emerald-400');
  });

  it('shows negative gap colored red when all-handled rejection is higher (non-informative signal)', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 20,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_unhandled_flag: 0.10,
        correctness_rejection_rate_with_all_handled: 0.35,
      },
    })} />);
    const gap = container.querySelector('[title*="positive = author\'s unhandled-flag signal is informative"]');
    expect(gap).not.toBeNull();
    expect(gap?.textContent).toBe('Δ−25pp');
    expect(gap?.className).toContain('text-red-400');
  });

  it('does not render rej w/unhandled-flag row when both sub-bucket fields are absent', () => {
    render(<EdgesTrendBreakdown trend={makeEdgesTrend()} />);
    expect(screen.queryByText('rej w/unhandled-flag vs all-handled')).toBeNull();
  });

  it('omits the gap when only one of the unhandled/all-handled sub-bucket rates is present', () => {
    const { container } = render(<EdgesTrendBreakdown trend={makeEdgesTrend({
      current: {
        sample_size: 10,
        tasks_with_edges_rate: 0.7,
        correctness_rejection_rate_with_unhandled_flag: 0.30,
      },
    })} />);
    expect(screen.getByText('rej w/unhandled-flag vs all-handled')).toBeTruthy();
    expect(container.querySelector('[title*="positive = author\'s unhandled-flag signal is informative"]')).toBeNull();
  });
});

describe('SrTrendBreakdown execution-evidence categories', () => {
  it('renders execution-evidence section when any of the three categories are present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.2, trend: 'stable' },
        tests_executed_cleanly: { miss_rate: 0.15, trend: 'stable' },
        lint_executed_cleanly: { miss_rate: 0.05, trend: 'increasing' },
        typecheck_executed_cleanly: { miss_rate: 0.1, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('execution-evidence-section')).toBeInTheDocument();
    expect(screen.getByText('Execution Evidence')).toBeInTheDocument();
    expect(screen.getByText('tests_executed_cleanly')).toBeInTheDocument();
    expect(screen.getByText('lint_executed_cleanly')).toBeInTheDocument();
    expect(screen.getByText('typecheck_executed_cleanly')).toBeInTheDocument();
  });

  it('does not render execution-evidence section when none of the three categories are present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.2, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('execution-evidence-section')).not.toBeInTheDocument();
  });

  it('execution-evidence categories are separated from regular categories in the card', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.2, trend: 'stable' },
        tests_executed_cleanly: { miss_rate: 0.15, trend: 'stable' },
      },
    };
    const { container } = render(<SrTrendBreakdown trends={trends} variant="card" />);
    const execSection = container.querySelector('[data-testid="execution-evidence-section"]');
    expect(execSection).not.toBeNull();
    // correctness should NOT be inside the execution-evidence section
    expect(execSection?.textContent).not.toContain('correctness');
    expect(execSection?.textContent).toContain('tests_executed_cleanly');
  });
});

describe('SrTrendBreakdown below_floor from backend', () => {
  it('uses backend below_floor field instead of deriving from rate_history', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.45,
          previous_hit_rate: 0.5,
          trend: 'decreasing',
          rate_history: [0.45],  // only 1 window — not enough to trigger isBelowFloor logic (needs 2+)
          below_floor: true,      // but backend says it's below floor
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const alertSpan = screen.getByLabelText(/below floor/i);
    expect(alertSpan).toBeInTheDocument();
  });

  it('falls back to local derivation when backend below_floor is absent', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.4,
          trend: 'decreasing',
          rate_history: [0.4, 0.45],  // 2 windows below 50% floor
          // no below_floor field — should derive locally
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const alertSpan = screen.getByLabelText(/below floor/i);
    expect(alertSpan).toBeInTheDocument();
  });

  it('does not show below-floor icon when backend sets below_floor: false despite history suggesting it', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.4,
          trend: 'decreasing',
          rate_history: [0.4, 0.45],  // would trigger isBelowFloor
          below_floor: false,           // but backend says no
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByLabelText(/below floor/i)).not.toBeInTheDocument();
  });
});

describe('SrTrendBreakdown srCatchRateTargets per-category floor', () => {
  it('shows below-floor icon when category is below configured target (no backend below_floor)', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          trend: 'stable',
          rate_history: [0.72, 0.74],  // above default 50% floor, but below configured 0.80
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    const alertSpan = screen.getByLabelText(/below floor/i);
    expect(alertSpan).toBeInTheDocument();
    expect(alertSpan.querySelector('svg')).toBeInTheDocument();
  });

  it('tooltip shows configured target percentage when srCatchRateTargets is set', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          trend: 'stable',
          rate_history: [0.72, 0.74],
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    const alertSpan = screen.getByLabelText(/below floor/i);
    expect(alertSpan.getAttribute('title')).toContain('80%');
    expect(alertSpan.getAttribute('title')).toContain('configured target');
  });

  it('does not show below-floor icon when category is above configured target', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.85,
          trend: 'stable',
          rate_history: [0.85, 0.87],  // above both default 50% and configured 0.80 target
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    expect(screen.queryByLabelText(/below floor/i)).not.toBeInTheDocument();
  });

  it('falls back to default 50% floor when srCatchRateTargets is not set', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.6,
          trend: 'stable',
          rate_history: [0.6, 0.62],  // above 50% floor — should NOT trigger
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByLabelText(/below floor/i)).not.toBeInTheDocument();
  });

  it('backend below_floor overrides srCatchRateTargets local derivation', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.72,
          trend: 'stable',
          rate_history: [0.72, 0.74],
          below_floor: true,  // backend forces the flag regardless of target
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ correctness: 0.80 }} />);
    expect(screen.getByLabelText(/below floor/i)).toBeInTheDocument();
  });
});

describe('SrTrendBreakdown by_fix_iteration section', () => {
  function makeIterTrends(byFixIteration: SrTrendData['by_fix_iteration']): SrTrendData {
    return {
      categories: { correctness: { miss_rate: 0.2, trend: 'stable' } },
      by_fix_iteration: byFixIteration,
    };
  }

  it('renders the by-fix-iteration section when by_fix_iteration has data', () => {
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.2, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-by-fix-iteration-section')).toBeInTheDocument();
  });

  it('does not render the section when by_fix_iteration is absent (backward compat)', () => {
    const trends = makeIterTrends(undefined);
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-by-fix-iteration-section')).not.toBeInTheDocument();
  });

  it('does not render the section when all buckets are empty', () => {
    const trends = makeIterTrends({ '1': {}, '2': {} });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-by-fix-iteration-section')).not.toBeInTheDocument();
  });

  it('section is collapsed by default; toggle opens it', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.2, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByText('iter 1')).not.toBeInTheDocument();

    const toggle = screen.getByRole('button', { name: /by fix iteration/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('iter 1')).toBeInTheDocument();
  });

  it('toggle closes section on second click', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.2, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const toggle = screen.getByRole('button', { name: /by fix iteration/i });
    await user.click(toggle);
    expect(screen.getByText('iter 1')).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.queryByText('iter 1')).not.toBeInTheDocument();
  });

  it('displays buckets in order 1 → 2 → 3+ when all present', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '3+': { correctness: { miss_rate: 0.6, sample_count: 5 } },
      '1': { correctness: { miss_rate: 0.2, sample_count: 20 } },
      '2': { correctness: { miss_rate: 0.4, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    const iterLabels = screen.getAllByText(/^iter \d[+]?$/);
    expect(iterLabels.map(el => el.textContent)).toEqual(['iter 1', 'iter 2', 'iter 3+']);
  });

  it('sorts categories by miss rate descending within a bucket', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': {
        correctness: { miss_rate: 0.1, sample_count: 10 },
        'error-handling': { miss_rate: 0.5, sample_count: 10 },
        'type-safety': { miss_rate: 0.3, sample_count: 10 },
      },
    });
    const { container } = render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    const section = container.querySelector('[data-testid="sr-by-fix-iteration-section"]');
    expect(section).not.toBeNull();
    const catSpans = Array.from(section!.querySelectorAll('span.text-neutral-400'));
    const texts = catSpans.map(el => el.textContent);
    const errIdx = texts.indexOf('error-handling');
    const typeIdx = texts.indexOf('type-safety');
    const corrIdx = texts.indexOf('correctness');
    expect(errIdx).toBeGreaterThanOrEqual(0);
    expect(errIdx).toBeLessThan(typeIdx);
    expect(typeIdx).toBeLessThan(corrIdx);
  });

  it('shows delta vs iter 1 for iter 2 when delta >= 5pp', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.2, sample_count: 20 } },
      '2': { correctness: { miss_rate: 0.35, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    // iter 2 correctness is 35% vs iter 1's 20% → delta +15pp
    const deltaEl = screen.getByTitle(/vs iter 1/i);
    expect(deltaEl).toHaveTextContent('+15pp');
    expect(deltaEl.className).toContain('text-red-400');
  });

  it('shows negative delta (green) when iter 2 miss rate improves vs iter 1', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.4, sample_count: 20 } },
      '2': { correctness: { miss_rate: 0.2, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    const deltaEl = screen.getByTitle(/vs iter 1/i);
    expect(deltaEl).toHaveTextContent('−20pp');
    expect(deltaEl.className).toContain('text-emerald-400');
  });

  it('omits delta when difference is < 5pp', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.2, sample_count: 20 } },
      '2': { correctness: { miss_rate: 0.23, sample_count: 10 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    expect(screen.queryByTitle(/vs iter 1/i)).not.toBeInTheDocument();
  });

  it('omits delta for iter 1 rows (no baseline to compare)', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '1': { correctness: { miss_rate: 0.3, sample_count: 20 } },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    expect(screen.queryByTitle(/vs iter 1/i)).not.toBeInTheDocument();
  });

  it('renders examples button when examples are present in a bucket category', async () => {
    const user = userEvent.setup();
    const trends = makeIterTrends({
      '2': {
        correctness: {
          miss_rate: 0.5,
          sample_count: 5,
          examples: [{ severity: 'CRITICAL', desc: 'null check missed in iter2' }],
        },
      },
    });
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    await user.click(screen.getByRole('button', { name: /by fix iteration/i }));
    const exBtn = screen.getByTitle(/5 samples — click for examples/i);
    await user.click(exBtn);
    expect(screen.getByText('null check missed in iter2')).toBeInTheDocument();
  });
});

describe('SrTrendBreakdown reasoning engagement', () => {
  it('displays reasoning engagement for regressing (decreasing) category', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.3,
          trend: 'decreasing',
          reasoning_engagement_rate: 0.4,
          reasoning_engagement_with: 4,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning thinning/i);
    expect(engEl).toHaveTextContent('eng:40%');
  });

  it('shows high engagement (80%) for regressing category with strong reasoning', () => {
    const trends: SrTrendData = {
      categories: {
        type_safety: {
          miss_rate: 0.15,
          trend: 'decreasing',
          reasoning_engagement_rate: 0.8,
          reasoning_engagement_with: 8,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:80%');
  });

  it('shows engagement for stable category when engagement data is present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.7,
          reasoning_engagement_with: 7,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:70%');
  });

  it('shows engagement for increasing category when engagement data is present', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,
          trend: 'increasing',
          reasoning_engagement_rate: 0.9,
          reasoning_engagement_with: 9,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:90%');
  });

  it('omits the engagement chip when total_passes is 0', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: null,
          reasoning_engagement_with: 0,
          reasoning_engagement_total: 0,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Reasoning engagement/i)).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Reasoning thinning/i)).not.toBeInTheDocument();
  });

  it('omits the engagement chip when engagement fields are absent', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.2, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Reasoning/i)).not.toBeInTheDocument();
  });

  it('shows amber thinning cue when rate < 0.5 AND total >= 10', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.3,
          reasoning_engagement_with: 3,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning thinning/i);
    expect(engEl).toHaveTextContent('eng:30%');
    expect(engEl.className).toContain('amber');
  });

  it('shows cyan (non-thinning) cue when rate >= 0.5', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.6,
          reasoning_engagement_with: 6,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:60%');
    expect(engEl.className).not.toContain('amber');
    expect(engEl.className).toContain('cyan');
  });

  it('does NOT show thinning cue when rate < 0.5 but total < 10 (insufficient sample)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.2,
          reasoning_engagement_with: 1,
          reasoning_engagement_total: 5,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    // chip should still show (total > 0) but NOT amber (insufficient sample)
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:20%');
    expect(engEl.className).not.toContain('amber');
  });

  it('displays engagement in inline variant for regressing category', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.25,
          trend: 'decreasing',
          reasoning_engagement_rate: 0.5,
          reasoning_engagement_with: 5,
          reasoning_engagement_total: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="inline" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:50%');
  });

  it('backend engagement_thinning=true wins over local computation (even when rate > 0.5)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.7, // > 0.5, would NOT show thinning locally
          reasoning_engagement_with: 7,
          reasoning_engagement_total: 10,
          engagement_thinning: true, // backend flag overrides
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning thinning/i);
    expect(engEl).toHaveTextContent('eng:70%');
    expect(engEl.className).toContain('amber'); // thinning color
  });

  it('backend engagement_thinning=false wins over local computation (even when rate < 0.5)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.3, // < 0.5, would show thinning locally
          reasoning_engagement_with: 3,
          reasoning_engagement_total: 10,
          engagement_thinning: false, // backend flag overrides
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning engagement/i);
    expect(engEl).toHaveTextContent('eng:30%');
    expect(engEl.className).toContain('cyan'); // non-thinning color
  });

  it('falls back to local computation when backend engagement_thinning is absent (backward compat)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          reasoning_engagement_rate: 0.3,
          reasoning_engagement_with: 3,
          reasoning_engagement_total: 10,
          // engagement_thinning is absent
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const engEl = screen.getByTitle(/Reasoning thinning/i);
    expect(engEl).toHaveTextContent('eng:30%');
    expect(engEl.className).toContain('amber'); // shows thinning via local logic
  });
});

describe('SrTrendBreakdown severity calibration badge', () => {
  it('shows undercal badge when under_rated/total >= 30% and total >= 5', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          miss_rate: 0.2,
          trend: 'stable',
          severity_calibration: { under_rated: 6, over_rated: 1, matched: 3, total: 10 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const badge = screen.getByTitle(/Severity under-rated/i);
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent('undercal:60%');
  });

  it('applies amber color to undercal badge', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,
          trend: 'stable',
          severity_calibration: { under_rated: 4, over_rated: 0, matched: 6, total: 10 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const badge = screen.getByTitle(/Severity under-rated/i);
    expect(badge.className).toContain('text-amber-400');
  });

  it('does not show undercal badge when total < 5 (insufficient sample)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          severity_calibration: { under_rated: 3, over_rated: 0, matched: 1, total: 4 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Severity under-rated/i)).not.toBeInTheDocument();
  });

  it('does not show undercal badge when under_rated/total < 30%', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          severity_calibration: { under_rated: 1, over_rated: 2, matched: 7, total: 10 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Severity under-rated/i)).not.toBeInTheDocument();
  });

  it('does not show undercal badge when severity_calibration is null', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.2, trend: 'stable', severity_calibration: null },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Severity under-rated/i)).not.toBeInTheDocument();
  });

  it('shows undercal badge in inline variant when threshold is met', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          miss_rate: 0.15,
          trend: 'stable',
          severity_calibration: { under_rated: 12, over_rated: 2, matched: 6, total: 20 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="inline" />);
    const badge = screen.getByTitle(/Severity under-rated/i);
    expect(badge).toHaveTextContent('undercal:60%');
  });

  it('badge title mentions under_rated count, total, and suppression context', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.1,
          trend: 'stable',
          severity_calibration: { under_rated: 8, over_rated: 1, matched: 3, total: 12 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const badge = screen.getByTitle(/Severity under-rated/i);
    expect(badge.title).toContain('8/12');
    expect(badge.title).toMatch(/under-rating suppresses escalation/i);
  });

  it('shows exact 30% boundary: under_rated 3/10 = 30% is shown', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.2,
          trend: 'stable',
          severity_calibration: { under_rated: 3, over_rated: 2, matched: 5, total: 10 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTitle(/Severity under-rated/i)).toHaveTextContent('undercal:30%');
  });

  it('does not show badge when severity_calibration is absent from category', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Severity under-rated/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/undercal/)).not.toBeInTheDocument();
  });
});

describe('SrTrendBreakdown by_scope section', () => {
  it('renders By Scope heading and rows when by_scope is populated (card variant)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
      by_scope: {
        small: {
          correctness: { miss_rate: 0.1, sample_count: 8 },
        },
        large: {
          correctness: { miss_rate: 0.6, sample_count: 6 },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByText('By Scope')).toBeInTheDocument();
    const section = screen.getByTestId('sr-trend-by-scope');
    expect(section).toHaveTextContent('small');
    expect(section).toHaveTextContent('large');
    expect(section).toHaveTextContent('10%');
    expect(section).toHaveTextContent('60%');
  });

  it('renders scopes in canonical order small > medium > large', () => {
    const trends: SrTrendData = {
      categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
      by_scope: {
        large: { correctness: { miss_rate: 0.6, sample_count: 6 } },
        small: { correctness: { miss_rate: 0.1, sample_count: 8 } },
        medium: { correctness: { miss_rate: 0.3, sample_count: 7 } },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const section = screen.getByTestId('sr-trend-by-scope');
    const text = section.textContent ?? '';
    const small = text.indexOf('small');
    const medium = text.indexOf('medium');
    const large = text.indexOf('large');
    expect(small).toBeGreaterThanOrEqual(0);
    expect(small).toBeLessThan(medium);
    expect(medium).toBeLessThan(large);
  });

  it('omits the section when by_scope is missing or empty', () => {
    const trends: SrTrendData = {
      categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-trend-by-scope')).not.toBeInTheDocument();
    expect(screen.queryByText('By Scope')).not.toBeInTheDocument();
  });

  it('skips scopes whose category record is empty', () => {
    const trends: SrTrendData = {
      categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
      by_scope: {
        small: { correctness: { miss_rate: 0.1, sample_count: 8 } },
        medium: {},
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const section = screen.getByTestId('sr-trend-by-scope');
    expect(section).toHaveTextContent('small');
    expect(section).not.toHaveTextContent('medium');
  });

  it('invokes onScopeClick with the row scope when a scope row is activated', async () => {
    const user = userEvent.setup();
    const onScopeClick = vi.fn();
    const trends: SrTrendData = {
      categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
      by_scope: {
        small: { correctness: { miss_rate: 0.1, sample_count: 8 } },
        large: { correctness: { miss_rate: 0.6, sample_count: 6 } },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" onScopeClick={onScopeClick} />);
    const largeRow = screen.getByTestId('sr-trend-by-scope-row-large');
    await user.click(largeRow);
    expect(onScopeClick).toHaveBeenCalledWith('large');
  });

  it('reveals examples when a category cell with examples is clicked', async () => {
    const user = userEvent.setup();
    const trends: SrTrendData = {
      categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
      by_scope: {
        large: {
          correctness: {
            miss_rate: 0.6,
            sample_count: 6,
            examples: [
              { severity: 'CRITICAL', desc: 'large-scope miss example' },
            ],
          },
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const section = screen.getByTestId('sr-trend-by-scope');
    const button = section.querySelector('button');
    if (!button) throw new Error('expected at least one clickable category cell');
    await user.click(button);
    expect(section).toHaveTextContent('large-scope miss example');
  });
});

describe('SrTrendBreakdown with srCatchRateTargets', () => {
  it('renders with srCatchRateTargets prop passed through correctly', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.65,
          miss_rate: 0.35,
          trend: 'stable',
        },
        correctness: {
          current_hit_rate: 0.8,
          miss_rate: 0.2,
          trend: 'stable',
        },
      },
    };
    const targets = { security: 0.7, correctness: 0.75 };
    const { container } = render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={targets} />);
    // Verify that the component renders successfully with targets
    expect(container).toBeInTheDocument();
    // Verify the categories are rendered
    expect(screen.getByText(/security/i)).toBeInTheDocument();
    expect(screen.getByText(/correctness/i)).toBeInTheDocument();
  });

  it('shows target and gap inline when category is below target', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          miss_rate: 0.28,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    const targetSpan = screen.getByTitle(/Target: 80% hit rate/);
    expect(targetSpan).toBeInTheDocument();
    expect(targetSpan).toHaveTextContent('target 80%');
    expect(targetSpan).toHaveTextContent('−8pp');
  });

  it('applies warning color (red) when gap is negative (below target)', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          miss_rate: 0.28,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    const targetSpan = screen.getByTitle(/Target: 80% hit rate/);
    expect(targetSpan.className).toContain('text-red-400');
  });

  it('applies neutral color when target is met (gap is zero or positive)', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.85,
          miss_rate: 0.15,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    const targetSpan = screen.getByTitle(/Target: 80% hit rate/);
    expect(targetSpan.className).toContain('text-neutral-400');
    expect(targetSpan).toHaveTextContent('target 80%');
    expect(targetSpan).toHaveTextContent('5pp');
  });

  it('does not show target display when no target is configured', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          miss_rate: 0.28,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTitle(/Target:/)).not.toBeInTheDocument();
  });

  it('does not show target display when current_hit_rate is null', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          miss_rate: 0.28,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateTargets={{ security: 0.80 }} />);
    expect(screen.queryByTitle(/Target:/)).not.toBeInTheDocument();
  });

  it('shows target display in inline variant when target is configured', () => {
    const trends: SrTrendData = {
      categories: {
        security: {
          current_hit_rate: 0.72,
          miss_rate: 0.28,
          trend: 'stable',
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="inline" srCatchRateTargets={{ security: 0.80 }} />);
    const targetSpan = screen.getByTitle(/Target: 80% hit rate/);
    expect(targetSpan).toBeInTheDocument();
    expect(targetSpan).toHaveTextContent('target 80%');
    expect(targetSpan).toHaveTextContent('−8pp');
  });
});

describe('SrTrendBreakdown onConfigureTargets CTA', () => {
  const makeTrendsWithCategories = (): SrTrendData => ({
    categories: {
      correctness: {
        miss_rate: 0.2,
        trend: 'stable',
      },
    },
  });

  it('shows configure targets CTA when srCatchRateTargets is undefined', () => {
    render(<SrTrendBreakdown trends={makeTrendsWithCategories()} variant="card" />);
    expect(screen.getByTestId('configure-targets-cta')).toBeInTheDocument();
    expect(screen.getByText(/Configure SR catch-rate targets/)).toBeInTheDocument();
  });

  it('shows configure targets CTA when srCatchRateTargets is empty object', () => {
    render(<SrTrendBreakdown trends={makeTrendsWithCategories()} variant="card" srCatchRateTargets={{}} />);
    expect(screen.getByTestId('configure-targets-cta')).toBeInTheDocument();
  });

  it('shows configure targets CTA when srCatchRateTargets is null', () => {
    render(<SrTrendBreakdown trends={makeTrendsWithCategories()} variant="card" srCatchRateTargets={null} />);
    expect(screen.getByTestId('configure-targets-cta')).toBeInTheDocument();
  });

  it('hides configure targets CTA when srCatchRateTargets has at least one entry', () => {
    render(<SrTrendBreakdown trends={makeTrendsWithCategories()} variant="card" srCatchRateTargets={{ correctness: 0.8 }} />);
    expect(screen.queryByTestId('configure-targets-cta')).not.toBeInTheDocument();
  });

  it('invokes onConfigureTargets callback when CTA is clicked', async () => {
    const onConfigureTargets = vi.fn();
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithCategories()}
        variant="card"
        onConfigureTargets={onConfigureTargets}
      />,
    );
    const button = screen.getByTestId('configure-targets-cta');
    await userEvent.click(button);
    expect(onConfigureTargets).toHaveBeenCalledOnce();
  });

  it('does not show CTA in inline variant even when targets are empty', () => {
    render(<SrTrendBreakdown trends={makeTrendsWithCategories()} variant="inline" />);
    expect(screen.queryByTestId('configure-targets-cta')).not.toBeInTheDocument();
  });
});

describe('MissRateSparkline target reference line', () => {
  it('renders a dashed horizontal line when target is provided', () => {
    const { container } = render(
      <MissRateSparkline
        history={[0.9, 0.8, 0.75, 0.7]}
        width={100}
        height={50}
        stroke="rgb(100, 200, 255)"
        target={0.8}
      />
    );
    const line = container.querySelector('line');
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute('stroke', 'rgb(64, 64, 64)');
    expect(line).toHaveAttribute('stroke-dasharray', '2,2');
    expect(line).toHaveAttribute('x1', '0');
    expect(line).toHaveAttribute('x2', '100');
  });

  it('does not render a dashed line when target is null or undefined', () => {
    const { container: containerNull } = render(
      <MissRateSparkline
        history={[0.9, 0.8, 0.75, 0.7]}
        width={100}
        height={50}
        stroke="rgb(100, 200, 255)"
        target={null}
      />
    );
    expect(containerNull.querySelector('line')).not.toBeInTheDocument();

    const { container: containerUndefined } = render(
      <MissRateSparkline
        history={[0.9, 0.8, 0.75, 0.7]}
        width={100}
        height={50}
        stroke="rgb(100, 200, 255)"
      />
    );
    expect(containerUndefined.querySelector('line')).not.toBeInTheDocument();
  });

  it('includes target in aria-label when provided', () => {
    const targetHitRate = 0.75;
    const { container } = render(
      <MissRateSparkline
        history={[0.9, 0.8, 0.75, 0.7]}
        width={100}
        height={50}
        stroke="rgb(100, 200, 255)"
        target={targetHitRate}
        title="miss rate trend"
      />
    );
    const svg = container.querySelector('svg');
    const expectedPercent = Math.round(targetHitRate * 100);
    expect(svg).toHaveAttribute('aria-label', `miss rate trend — target: ${expectedPercent}% hit rate`);
  });
});

describe('computeTargetHealth', () => {
  it('returns null when no targets are configured', () => {
    const categories = { correctness: { current_hit_rate: 0.7 } };
    expect(computeTargetHealth(categories, null)).toBeNull();
    expect(computeTargetHealth(categories, {})).toBeNull();
  });

  it('returns healthy stats when all categories are above target', () => {
    const categories = {
      correctness: { current_hit_rate: 0.9, previous_hit_rate: 0.85 },
      security: { current_hit_rate: 0.8, previous_hit_rate: 0.75 },
    };
    const targets = { correctness: 0.8, security: 0.7 };
    const stats = computeTargetHealth(categories, targets);
    expect(stats).not.toBeNull();
    expect(stats!.configured).toBe(2);
    expect(stats!.belowTarget).toBe(0);
    expect(stats!.newlyMissed).toBe(0);
    expect(stats!.newlyRecovered).toBe(0);
    expect(stats!.hasCycleData).toBe(true);
  });

  it('counts categories below their target', () => {
    const categories = {
      correctness: { current_hit_rate: 0.6, previous_hit_rate: 0.65 },
      security: { current_hit_rate: 0.9, previous_hit_rate: 0.85 },
    };
    const targets = { correctness: 0.8, security: 0.8 };
    const stats = computeTargetHealth(categories, targets);
    expect(stats!.belowTarget).toBe(1);
    expect(stats!.configured).toBe(2);
  });

  it('detects newly missed category (was above, now below)', () => {
    const categories = {
      correctness: { current_hit_rate: 0.6, previous_hit_rate: 0.85 },
    };
    const targets = { correctness: 0.8 };
    const stats = computeTargetHealth(categories, targets);
    expect(stats!.newlyMissed).toBe(1);
    expect(stats!.newlyRecovered).toBe(0);
    expect(stats!.belowTarget).toBe(1);
  });

  it('detects newly recovered category (was below, now above)', () => {
    const categories = {
      correctness: { current_hit_rate: 0.85, previous_hit_rate: 0.6 },
    };
    const targets = { correctness: 0.8 };
    const stats = computeTargetHealth(categories, targets);
    expect(stats!.newlyRecovered).toBe(1);
    expect(stats!.newlyMissed).toBe(0);
    expect(stats!.belowTarget).toBe(0);
  });

  it('reports hasCycleData=false when no previous_hit_rate is available', () => {
    const categories = {
      correctness: { current_hit_rate: 0.6 },
    };
    const targets = { correctness: 0.8 };
    const stats = computeTargetHealth(categories, targets);
    expect(stats!.hasCycleData).toBe(false);
    expect(stats!.newlyMissed).toBe(0);
    expect(stats!.newlyRecovered).toBe(0);
    expect(stats!.belowTarget).toBe(1);
  });
});

describe('TargetHealthSummaryCard in SrTrendBreakdown', () => {
  function makeTrendsWithTargets(overrides?: Partial<SrTrendData>): SrTrendData {
    return {
      categories: {
        correctness: { current_hit_rate: 0.9, previous_hit_rate: 0.85, trend: 'stable' },
        security: { current_hit_rate: 0.75, previous_hit_rate: 0.8, trend: 'stable' },
      },
      ...overrides,
    };
  }

  it('renders target health summary card when targets are configured', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets()}
        variant="card"
        srCatchRateTargets={{ correctness: 0.8, security: 0.7 }}
      />
    );
    expect(screen.getByTestId('target-health-summary')).toBeInTheDocument();
    expect(screen.getByText('2 configured')).toBeInTheDocument();
  });

  it('does not render target health summary when no targets are configured', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets()}
        variant="card"
        srCatchRateTargets={{}}
      />
    );
    expect(screen.queryByTestId('target-health-summary')).not.toBeInTheDocument();
  });

  it('shows zero below-target when all categories meet their target', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets()}
        variant="card"
        srCatchRateTargets={{ correctness: 0.8, security: 0.7 }}
      />
    );
    expect(screen.getByText('0 below target')).toBeInTheDocument();
    expect(screen.queryByTestId('newly-missed-indicator')).not.toBeInTheDocument();
    expect(screen.queryByTestId('newly-recovered-indicator')).not.toBeInTheDocument();
  });

  it('shows below-target count and newly-missed indicator when category crosses below target', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets({
          categories: {
            correctness: { current_hit_rate: 0.6, previous_hit_rate: 0.85, trend: 'decreasing' },
            security: { current_hit_rate: 0.9, previous_hit_rate: 0.85, trend: 'stable' },
          },
        })}
        variant="card"
        srCatchRateTargets={{ correctness: 0.8, security: 0.8 }}
      />
    );
    expect(screen.getByText('1 below target')).toBeInTheDocument();
    expect(screen.getByTestId('newly-missed-indicator')).toBeInTheDocument();
    expect(screen.queryByTestId('newly-recovered-indicator')).not.toBeInTheDocument();
  });

  it('shows newly-recovered indicator when category crosses above target', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets({
          categories: {
            correctness: { current_hit_rate: 0.85, previous_hit_rate: 0.6, trend: 'increasing' },
          },
        })}
        variant="card"
        srCatchRateTargets={{ correctness: 0.8 }}
      />
    );
    expect(screen.getByText('0 below target')).toBeInTheDocument();
    expect(screen.getByTestId('newly-recovered-indicator')).toBeInTheDocument();
    expect(screen.queryByTestId('newly-missed-indicator')).not.toBeInTheDocument();
  });

  it('does not show cycle-delta indicators when only one rate_history window exists (no previous_hit_rate)', () => {
    render(
      <SrTrendBreakdown
        trends={makeTrendsWithTargets({
          categories: {
            correctness: { current_hit_rate: 0.6, trend: 'stable' },
          },
        })}
        variant="card"
        srCatchRateTargets={{ correctness: 0.8 }}
      />
    );
    expect(screen.getByTestId('target-health-summary')).toBeInTheDocument();
    expect(screen.getByText('1 below target')).toBeInTheDocument();
    expect(screen.queryByTestId('newly-missed-indicator')).not.toBeInTheDocument();
    expect(screen.queryByTestId('newly-recovered-indicator')).not.toBeInTheDocument();
  });
});

describe('SrTrendBreakdown finding-count gating (catch-rate true denominator)', () => {
  it('shows too-small with reviewer finding count when current_reviewer_finding_count < MIN_SAMPLE_SIZE even if current_human_count is large', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.8,
          previous_hit_rate: 0.6,
          miss_rate: 0.2,
          trend: 'stable',
          current_human_count: 50,  // many tasks
          current_reviewer_finding_count: 3,  // but very few reviewer findings
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByText(/too small/)).toBeInTheDocument();
    // Should show finding count (3), not task count (50)
    expect(screen.getByText(/3 — too small/)).toBeInTheDocument();
  });

  it('suppresses cycle-over-cycle delta when previous window has too few findings', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.9,
          previous_hit_rate: 0.5,
          miss_rate: 0.1,
          trend: 'increasing',
          current_reviewer_finding_count: 20,  // enough in current window
          previous_reviewer_finding_count: 2,  // but too few in previous window
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    // delta would be +40pp but previous window is too small to trust
    expect(screen.queryByTitle(/cycle-over-cycle change/)).not.toBeInTheDocument();
  });

  it('shows delta only when both current and previous finding counts are sufficient', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          current_hit_rate: 0.9,
          previous_hit_rate: 0.7,
          miss_rate: 0.1,
          trend: 'increasing',
          current_reviewer_finding_count: 15,
          previous_reviewer_finding_count: 10,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTitle(/cycle-over-cycle change/)).toBeInTheDocument();
  });

  it('falls back to current_human_count when current_finding_count is absent (backward compat)', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.3,
          trend: 'stable',
          current_human_count: 3,  // no finding_count — falls back to human_count
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByText(/too small/)).toBeInTheDocument();
    expect(screen.getByText(/3 — too small/)).toBeInTheDocument();
  });

  it('falls back to current_finding_count when reviewer-grounded aliases are absent', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: {
          miss_rate: 0.3,
          trend: 'stable',
          current_finding_count: 3,
        },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByText(/too small/)).toBeInTheDocument();
    expect(screen.getByText(/3 — too small/)).toBeInTheDocument();
  });
});

describe('SrTrendBreakdown data_sufficient / frozen badge', () => {
  const baseTrends: SrTrendData = {
    categories: { correctness: { miss_rate: 0.3, trend: 'stable' } },
  };

  it('renders frozen badge in card variant when data_sufficient is false', () => {
    const trends: SrTrendData = {
      ...baseTrends,
      data_sufficient: false,
      last_sufficient_computed_at: '2026-05-01T00:00:00',
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const badge = screen.getByTestId('sr-trend-insufficient-badge');
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toMatch(/frozen/);
    expect(badge.textContent).toContain(new Date('2026-05-01T00:00:00').toLocaleDateString());
  });

  it('renders frozen badge in inline variant when data_sufficient is false', () => {
    const trends: SrTrendData = {
      ...baseTrends,
      data_sufficient: false,
      last_sufficient_computed_at: '2026-05-01T00:00:00',
    };
    render(<SrTrendBreakdown trends={trends} variant="inline" />);
    const badge = screen.getByTestId('sr-trend-insufficient-badge');
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toMatch(/frozen/);
  });

  it('does not render frozen badge when data_sufficient is true', () => {
    const trends: SrTrendData = {
      ...baseTrends,
      data_sufficient: true,
      last_sufficient_computed_at: '2026-05-21T00:00:00',
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-trend-insufficient-badge')).not.toBeInTheDocument();
  });

  it('does not render frozen badge when data_sufficient is absent', () => {
    render(<SrTrendBreakdown trends={baseTrends} variant="card" />);
    expect(screen.queryByTestId('sr-trend-insufficient-badge')).not.toBeInTheDocument();
  });

  it('falls back to computed_at when last_sufficient_computed_at is null', () => {
    const trends: SrTrendData = {
      ...baseTrends,
      data_sufficient: false,
      computed_at: '2026-04-15T00:00:00',
      last_sufficient_computed_at: null,
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    const badge = screen.getByTestId('sr-trend-insufficient-badge');
    expect(badge.textContent).toContain(new Date('2026-04-15T00:00:00').toLocaleDateString());
  });

  it('shows both stale and frozen badges simultaneously when both conditions hold', () => {
    const trends: SrTrendData = {
      ...baseTrends,
      is_stale: true,
      computed_at: '2026-04-01T00:00:00',
      data_sufficient: false,
      last_sufficient_computed_at: '2026-03-20T00:00:00',
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-trend-stale-badge')).toBeInTheDocument();
    expect(screen.getByTestId('sr-trend-insufficient-badge')).toBeInTheDocument();
  });
});

describe('SrTrendBreakdown is_stale badge', () => {
  it('renders stale badge in card variant when is_stale is true', () => {
    const trends: SrTrendData = {
      is_stale: true,
      computed_at: '2026-04-01T00:00:00',
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-trend-stale-badge')).toBeInTheDocument();
    expect(screen.getByTestId('sr-trend-stale-badge').textContent).toMatch(/stale/);
  });

  it('does not render stale badge in card variant when is_stale is false', () => {
    const trends: SrTrendData = {
      is_stale: false,
      computed_at: '2026-05-21T00:00:00',
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-trend-stale-badge')).toBeNull();
  });

  it('does not render stale badge in card variant when is_stale is absent', () => {
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.queryByTestId('sr-trend-stale-badge')).toBeNull();
  });

  it('renders stale badge in inline variant when is_stale is true', () => {
    const trends: SrTrendData = {
      is_stale: true,
      computed_at: '2026-04-01T00:00:00',
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="inline" />);
    expect(screen.getByTestId('sr-trend-stale-badge')).toBeInTheDocument();
    expect(screen.getByTestId('sr-trend-stale-badge').textContent).toContain('[stale]');
  });
});

describe('SrCatchRateFreshnessPill', () => {
  it('renders healthy, stale, low-data, and missing states with stable labels and tooltips', () => {
    const healthStates = [
      {
        health: {
          status: 'healthy' as const,
          data_sufficient: true,
          last_sufficient_computed_at: '2026-05-27T00:00:00Z',
          last_sufficient_age_hours: 1.2,
          signal_last_computed_at: '2026-05-27T00:00:00Z',
          signal_age_hours: 1.2,
          category_count: 4,
          categories_with_hit_rate: 4,
          overall_hit_rate: 0.75,
        },
        label: 'fresh',
        tooltip: 'Fresh snapshot',
      },
      {
        health: {
          status: 'stale' as const,
          data_sufficient: true,
          last_sufficient_computed_at: '2026-05-25T00:00:00Z',
          last_sufficient_age_hours: 30,
          signal_last_computed_at: '2026-05-27T00:00:00Z',
          signal_age_hours: 2,
          category_count: 4,
          categories_with_hit_rate: 3,
          overall_hit_rate: 0.62,
        },
        label: 'snapshot stale (30h)',
        tooltip: 'Snapshot stale',
      },
      {
        health: {
          status: 'low_data' as const,
          data_sufficient: false,
          last_sufficient_computed_at: '2026-05-27T00:00:00Z',
          last_sufficient_age_hours: 1.5,
          signal_last_computed_at: '2026-05-27T00:00:00Z',
          signal_age_hours: 1.5,
          category_count: 5,
          categories_with_hit_rate: 1,
          overall_hit_rate: 0.4,
        },
        label: 'low data (n=1)',
        tooltip: 'Low data',
      },
      {
        health: {
          status: 'missing' as const,
          data_sufficient: false,
          last_sufficient_computed_at: null,
          last_sufficient_age_hours: null,
          signal_last_computed_at: null,
          signal_age_hours: null,
          category_count: 0,
          categories_with_hit_rate: 0,
          overall_hit_rate: null,
        },
        label: 'no snapshot',
        tooltip: 'No snapshot',
      },
    ];

    healthStates.forEach(({ health, label, tooltip }) => {
      const { unmount } = render(<SrCatchRateFreshnessPill health={health} />);
      const pill = screen.getByText(label);
      expect(pill).toBeInTheDocument();
      const wrapper = pill.closest('[data-testid]');
      if (!wrapper) throw new Error('Expected freshness pill wrapper');
      expect(wrapper).toHaveAttribute('title', expect.stringContaining(tooltip));
      expect(wrapper).toHaveAttribute('title', expect.stringContaining('sample'));
      unmount();
    });
  });
});

describe('SrTrendBreakdown catch-rate health header', () => {
  it('wires the freshness pill into the card and inline headers', () => {
    const health = {
      status: 'stale' as const,
      data_sufficient: true,
      last_sufficient_computed_at: '2026-05-25T00:00:00Z',
      last_sufficient_age_hours: 30,
      signal_last_computed_at: '2026-05-27T00:00:00Z',
      signal_age_hours: 2,
      category_count: 4,
      categories_with_hit_rate: 3,
      overall_hit_rate: 0.62,
    };
    const trends: SrTrendData = {
      categories: {
        correctness: { miss_rate: 0.3, trend: 'stable' },
      },
    };

    const { rerender } = render(<SrTrendBreakdown trends={trends} variant="card" srCatchRateHealth={health} />);
    expect(screen.getByTestId('sr-catch-rate-health-stale')).toHaveTextContent('snapshot stale (30h)');
    expect(screen.getByTestId('sr-catch-rate-health-stale')).toHaveAttribute('title', expect.stringContaining('signal age 2h'));

    rerender(<SrTrendBreakdown trends={trends} variant="inline" srCatchRateHealth={health} />);
    expect(screen.getByTestId('sr-catch-rate-health-stale')).toHaveTextContent('snapshot stale (30h)');
  });
});

describe('SrTrendData overall verdict fields', () => {
  it('SrTrendData accepts overall_hit_rate and overall_trend_verdict fields', () => {
    const trends: SrTrendData = {
      overall_hit_rate: 0.72,
      previous_overall_hit_rate: 0.68,
      overall_trend_verdict: 'improving',
      overall_trend_significant: true,
      categories: {
        correctness: { miss_rate: 0.28, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-trend-breakdown')).toBeInTheDocument();
  });

  it('SrTrendData accepts null overall verdict when data is insufficient', () => {
    const trends: SrTrendData = {
      overall_hit_rate: 0.65,
      previous_overall_hit_rate: null,
      overall_trend_verdict: null,
      overall_trend_significant: null,
      data_sufficient: false,
      categories: {
        correctness: { miss_rate: 0.35, trend: 'stable' },
      },
    };
    render(<SrTrendBreakdown trends={trends} variant="card" />);
    expect(screen.getByTestId('sr-trend-insufficient-badge')).toBeInTheDocument();
  });
});
