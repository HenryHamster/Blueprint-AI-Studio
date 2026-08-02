import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { act } from 'react';
import MetricsDashboard from './MetricsDashboard';
import type { Task } from '../types/task';
import { __pollerInternals } from '../hooks/usePoller';

const { apiFetch } = vi.hoisted(() => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/lane-diag')) {
      return new Response(JSON.stringify({
        pr_count: 5,
        task_count: 20,
        truncated: false,
        pr_breakdown_limit: 100,
        pr_breakdown_truncated: false,
        window_days: 7,
        window_start: '2026-04-17T12:00:00Z',
        project_id: 'project-1',
        max_tasks: 5000,
        relay_counts_by_lane: { LANE_CI: 3, LANE_CONFLICT: 1, LANE_REVIEW_CONVERGENCE: 2 },
        lane_iteration_distribution: {},
        per_pr_phase_breakdown: [],
        lane_waste: {
          waste_taxonomy: { infra: 2, merge_conflict: 1, ci_churn: 3, review_ambiguity: 0, lost_reasoning: 1, unknown: 0 },
          waste_task_count: 7,
          cap_counted_task_count: 7,
          cap_excluded_task_count: 0,
          chain_count: 4,
          cap_exhausted_chain_count: 0,
          chain_limit: 50,
          chains_truncated: false,
        },
      }), { status: 200 });
    }
    if (url.includes('/task-cost-breakdown')) {
      return new Response(JSON.stringify({
        project_id: 'project-1',
        window: '6h',
        window_ms: 21600000,
        window_start: '2026-04-24T06:00:00Z',
        task_count: 3,
        total_estimated_cost_usd: 24.5,
        task_type_breakdown: [
          { task_type: 'research', task_count: 2, estimated_cost_usd: 17.5 },
          { task_type: 'create', task_count: 1, estimated_cost_usd: 7.0 },
        ],
        direction_breakdown: [
          { direction_id: 'dir-123', task_count: 2, estimated_cost_usd: 17.5 },
          { direction_id: null, task_count: 1, estimated_cost_usd: 7.0 },
        ],
        top_tasks: [
          {
            task_id: 'task-1',
            task_title: 'Research expensive thing',
            task_type: 'research',
            direction_id: 'dir-123',
            completed_at: '2026-04-24T11:00:00Z',
            estimated_cost_usd: 12.5,
          },
          {
            task_id: 'task-2',
            task_title: 'Another expensive task',
            task_type: 'create',
            direction_id: null,
            completed_at: '2026-04-24T10:30:00Z',
            estimated_cost_usd: 7.0,
          },
        ],
      }), { status: 200 });
    }
    if (url.includes('/digest-latency-stats')) {
      return new Response(JSON.stringify({
        latency_percentiles: { p50_seconds: 0.42, p95_seconds: 1.87, sample_n: 12 },
        tier_counts: { l1_precompute: 7, l2_shared_cache: 3, l3_on_demand: 2 },
        total_served: 12,
        instance_note: 'per-instance aggregate; does not sum across replicas',
      }), { status: 200 });
    }
    if (url.includes('/spend-burn-rate')) {
      return new Response(JSON.stringify({
        project_id: null,
        window_days: 7,
        window_cost_usd: 100.00,
        task_count: 42,
        daily_spend_usd: 14.28,
        monthly_forecast_usd: 435.00,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({
      axes: {
        cost_per_merged_pr: {
          current: 45.2, baseline: 50.0, drift_pct: -9.6,
          history: [{ captured_at: '2026-05-01T00:00:00Z', value: 45.2 }],
        },
        vm_cold_start_p50_ms: {
          current: 3200, baseline: 3500, drift_pct: -8.6,
          history: [{ captured_at: '2026-05-01T00:00:00Z', value: 3200 }],
        },
        vm_reuse_rate: { current: 72, baseline: 68, drift_pct: 5.9, history: [] },
        median_api_calls_per_task: { current: 12, baseline: 15, drift_pct: -20.0, history: [{ captured_at: '2026-05-01T00:00:00Z', value: 12 }] },
        median_review_iterations: { current: 2.1, baseline: 2.5, drift_pct: -16.0 },
      },
      baseline_captured_at: '2026-05-01T00:00:00Z',
      last_snapshot_at: '2026-05-08T00:00:00Z',
      format_compliance: { format_compliance_rate: { current: 0.5, baseline: 0.4, drift_pct: 25.0, history: [{ captured_at: '2026-05-01T00:00:00Z', value: 0.5 }] }, self_repair_success_rate: { current: 0.75, baseline: 0.5, drift_pct: 50.0, history: [{ captured_at: '2026-05-01T00:00:00Z', value: 0.75 }] }, tasks_measured: 8, well_formed_tasks: 4, format_error_total: 4, repair_attempts: 2, repair_successes: 2 },
      agent_timeout_rate: { overall: { current: 0.125, baseline: 0.25, drift_pct: -50.0, history: [{ captured_at: '2026-05-01T00:00:00Z', value: 0.125 }] }, by_subtype: { execution_timeout: { current: 0.125, baseline: 0.25, drift_pct: -50.0 } }, dominant_subtype: 'execution_timeout', dominant_share: 0.75, denominator: 8, denominator_definition: 'timed-out tasks / completed-or-failed tasks', timeout_count: 1, window_days: 7 },
      warm_start_origin_breakdown: {
        warm_pool: 9,
        backlog_reuse: 3,
        chain_continuity: 4,
        warm_total: 16,
        pool_origin_share: 0.5625,
      },
    }), { status: 200 });
  }),
}));

vi.mock('../api', () => ({
  apiFetch,
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null,
  Legend: () => null,
}));

function makeTask(status: Task['status'], created_at: string): Task {
  return {
    id: `${status}-${created_at}`,
    title: status,
    description: status,
    status,
    created_by: 'agent',
    requires_approval: false,
    created_at,
  };
}

describe('MetricsDashboard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
    apiFetch.mockClear();
    __pollerInternals.reset();
  });

  afterEach(() => {
    __pollerInternals.reset();
    vi.useRealTimers();
  });

  it('renders empty-state charts and summary cards without crashing', () => {
    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="1h"
        onWindowChange={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Collecting data…').length).toBeGreaterThan(0);
    expect(screen.getByText('Total VM-Hours')).toBeInTheDocument();
    expect(screen.getByText('Tasks Completed')).toBeInTheDocument();
    expect(screen.getByText('Success Rate')).toBeInTheDocument();
  });

  it('shows values, threshold styling, and window selection controls', () => {
    const onWindowChange = vi.fn();

    render(
      <MetricsDashboard
        metricsHistory={[
          { timestamp: Date.now() - 600000, activeVMs: 2, costPerHr: 1.25, completedCount: 2, failedCount: 0 },
          { timestamp: Date.now() - 300000, activeVMs: 3, costPerHr: 2.5, completedCount: 2, failedCount: 1 },
        ]}
        cumulativeCost={12.34}
        cumulativeVmHours={56.78}
        maxConcurrentVMs={4}
        tasks={[
          makeTask('COMPLETED', '2026-04-24T11:50:00Z'),
          makeTask('COMPLETED', '2026-04-24T11:40:00Z'),
          makeTask('COMPLETED', '2026-04-24T08:00:00Z'),
          makeTask('COMPLETED', '2026-04-24T02:00:00Z'),
          makeTask('FAILED', '2026-04-24T09:30:00Z'),
        ]}
        metricsWindow="1h"
        onWindowChange={onWindowChange}
      />,
    );

    expect(screen.getByText('56.78')).toBeInTheDocument();
    expect(screen.getByText('Cumulative VM Cost:')).toBeInTheDocument();
    expect(screen.getByText('$12.34')).toBeInTheDocument();
    expect(screen.getByText('2', { exact: true })).toBeInTheDocument();

    expect(screen.getByText('100.0%').className).toContain('text-emerald-400');
    expect(screen.getByText('80.0%').className).toContain('text-amber-400');

    fireEvent.click(screen.getByRole('button', { name: '6h' }));
    expect(onWindowChange).toHaveBeenCalledWith('6h');
  });

  it('renders digest latency p50/p95, tier mix, and per-instance caveat', async () => {
    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="1h"
        onWindowChange={vi.fn()}
      />,
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText('Digest Latency')).toBeInTheDocument();
    expect(screen.getByText('0.42s')).toBeInTheDocument();
    expect(screen.getByText('1.87s')).toBeInTheDocument();
    expect(screen.getByText('7 | 58%')).toBeInTheDocument();
    expect(screen.getByText('3 | 25%')).toBeInTheDocument();
    expect(screen.getByText('2 | 17%')).toBeInTheDocument();
    expect(screen.getByTestId('digest-latency-caveat')).toHaveTextContent('per-instance aggregate');
  });

  it('exports the currently filtered metric series as CSV and JSON', () => {
    const originalCreateElement = document.createElement.bind(document);
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:metrics');
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const click = vi.fn();
    vi.spyOn(document, 'createElement').mockImplementation(((tagName: string) =>
      tagName === 'a' ? ({ click, set href(_value: string) {}, set download(_value: string) {} } as unknown as HTMLAnchorElement) : originalCreateElement(tagName)) as typeof document.createElement);

    render(
      <MetricsDashboard
        metricsHistory={[
          { timestamp: Date.now() - 4000000, activeVMs: 1, costPerHr: 1, completedCount: 1, failedCount: 0 },
          { timestamp: Date.now() - 300000, activeVMs: 2, costPerHr: 2, completedCount: 3, failedCount: 1 },
        ]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="1h"
        onWindowChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'CSV' }));
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(click).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'JSON' }));
    expect(click).toHaveBeenCalledTimes(2);
    expect(revokeObjectURL).toHaveBeenCalledTimes(2);
  });

  it('fetches and renders project-scoped cost breakdown rows', async () => {
    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={12.34}
        cumulativeVmHours={56.78}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="6h"
        onWindowChange={vi.fn()}
        projectId="project-1"
      />,
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(apiFetch).toHaveBeenCalledWith(
      '/api/admin/task-cost-breakdown?project_id=project-1&window=6h&top_n=5',
    );
    expect(screen.getByText('Cost Breakdown')).toBeInTheDocument();
    expect(screen.getByText('By task type')).toBeInTheDocument();
    expect(screen.getAllByText('research').length).toBeGreaterThan(0);
    expect(screen.getAllByText('dir-123').length).toBeGreaterThan(0);
    expect(screen.getByText('Research expensive thing')).toBeInTheDocument();
    expect(screen.getByText('$24.50 across 3 tasks')).toBeInTheDocument();
  });

  it('renders the warm-start per-origin breakdown alongside the vm_reuse_rate card', async () => {
    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="6h"
        onWindowChange={vi.fn()}
        projectId="project-1"
      />,
    );

    await act(async () => {
      await Promise.resolve();
    });

    // The truthful per-origin companion renders inside the Efficiency Axes
    // widget, subordinating the contaminated aggregate reuse rate.
    expect(screen.getByTestId('warm-start-origin-breakdown')).toBeInTheDocument();
    const share = screen.getByTestId('warm-start-pool-share');
    expect(share).toHaveTextContent('56%'); // round(0.5625 * 100)

    // The aggregate vm_reuse_rate is explicitly captioned as contaminated so the
    // operator is steered to the per-origin split rather than the headline rate.
    const caveat = screen.getByTestId('vm-reuse-aggregate-caveat');
    expect(caveat).toHaveTextContent(/conflates/i);
    expect(caveat).toHaveTextContent(/pool share/i);
  });

  it('renders optional nested efficiency blocks when present and omits them when absent', async () => {
    const renderDashboard = () => render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="6h"
        onWindowChange={vi.fn()}
        projectId="project-1"
      />,
    );

    const { unmount } = renderDashboard();
    await act(async () => {
      await Promise.resolve();
    });

    const formatCompliance = screen.getByTestId('format-compliance-block');
    expect(formatCompliance).toHaveTextContent('50.0%');
    expect(formatCompliance).toHaveTextContent('75.0%');

    const timeoutRate = screen.getByTestId('agent-timeout-rate-block');
    expect(timeoutRate).toHaveTextContent('12.5%');
    expect(timeoutRate).toHaveTextContent('dominant subtype: execution_timeout');

    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    unmount();
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({ axes: { cost_per_merged_pr: { current: 45.2, baseline: 50.0, drift_pct: -9.6, history: [] } }, baseline_captured_at: '2026-05-01T00:00:00Z', last_snapshot_at: '2026-05-08T00:00:00Z' }), { status: 200 });
      }
      return original(url);
    });

    renderDashboard();
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByTestId('format-compliance-block')).toBeNull();
    expect(screen.queryByTestId('agent-timeout-rate-block')).toBeNull();
    apiFetch.mockImplementation(original);
  });

  it('renders per-task-type format compliance breakdown when format_compliance_by_type is present', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: { cost_per_merged_pr: { current: 45.2, baseline: 50.0, drift_pct: -9.6, history: [] } },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
          format_compliance: {
            format_compliance_rate: { current: 0.85, baseline: 0.8, drift_pct: 6.25, history: [] },
            self_repair_success_rate: { current: 0.9, baseline: 0.85, drift_pct: 5.88, history: [] },
            tasks_measured: 10, well_formed_tasks: 9, format_error_total: 1, repair_attempts: 1, repair_successes: 1,
          },
          format_compliance_by_type: {
            create: { tasks_measured: 6, well_formed_tasks: 6, format_compliance_rate: 1.0, format_error_total: 0, repair_attempts: 0, repair_successes: 0, self_repair_success_rate: null },
            review: { tasks_measured: 4, well_formed_tasks: 3, format_compliance_rate: 0.75, format_error_total: 1, repair_attempts: 1, repair_successes: 1, self_repair_success_rate: 1.0 },
          },
        }), { status: 200 });
      }
      return original(url);
    });

    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="6h"
        onWindowChange={vi.fn()}
        projectId="project-1"
      />,
    );
    await act(async () => { await Promise.resolve(); });

    const byTypeBlock = screen.getByTestId('format-compliance-by-type-block');
    expect(byTypeBlock).toHaveTextContent('create');
    expect(byTypeBlock).toHaveTextContent('100%');
    expect(byTypeBlock).toHaveTextContent('review');
    expect(byTypeBlock).toHaveTextContent('75%');
    // format_error_total > 0 for review → error indicator rendered
    expect(byTypeBlock).toHaveTextContent('1 err');
    // create has format_error_total == 0 → no error label
    expect(byTypeBlock).not.toHaveTextContent('0 err');
    apiFetch.mockImplementation(original);
  });

  it('renders efficiency axis baseline provenance when seeded_at differs from baseline_captured_at', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: {
            cost_per_merged_pr: {
              current: 40.0,
              baseline: 50.0,
              drift_pct: -20.0,
              history: [],
              baseline_seeded_at: '2026-05-15T00:00:00Z',
              baseline_seed_snapshot_at: '2026-05-15T00:00:00Z',
            },
            vm_reuse_rate: {
              current: 72,
              baseline: 68,
              drift_pct: 5.9,
              history: [],
            },
          },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-20T00:00:00Z',
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );

      await act(async () => {
        await Promise.resolve();
      });

      // Axis that was re-seeded after the top-level baseline shows provenance
      const provenance = screen.getByTestId('axis-reseed-provenance-cost_per_merged_pr');
      expect(provenance).toBeInTheDocument();
      expect(provenance).toHaveTextContent(/baseline re-seeded/i);
      expect(provenance).toHaveTextContent(/snapshot/i);

      // Axis without re-seed provenance does not show the provenance element
      expect(screen.queryByTestId('axis-reseed-provenance-vm_reuse_rate')).toBeNull();
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

  it('renders cleanly when per-axis provenance fields are absent (older payload)', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: {
            cost_per_merged_pr: {
              current: 45.2,
              baseline: 50.0,
              drift_pct: -9.6,
              history: [],
              // No baseline_seeded_at or baseline_seed_snapshot_at — older payload
            },
          },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );

      await act(async () => {
        await Promise.resolve();
      });

      // Baseline value still renders
      expect(screen.getByText(/baseline: 50.0\$/)).toBeInTheDocument();
      // No provenance row rendered
      expect(screen.queryByTestId('axis-reseed-provenance-cost_per_merged_pr')).toBeNull();
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

  it('renders llm cost by terminal outcome block when llm_cost_attribution is present', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: { cost_per_merged_pr: { current: 45.2, baseline: 50.0, drift_pct: -9.6, history: [] } },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
          llm_cost_attribution: {
            total_llm_usd: 3.45,
            by_failure_class: [
              { failure_class: 'MERGED', task_count: 12, total_cost_usd: 2.10 },
              { failure_class: 'CODE_ERROR', task_count: 4, total_cost_usd: 0.85 },
              { failure_class: 'AGENT_TIMEOUT', task_count: 2, total_cost_usd: 0.50 },
            ],
          },
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );
      await act(async () => { await Promise.resolve(); });

      const block = screen.getByTestId('llm-cost-by-outcome-block');
      expect(block).toHaveTextContent('LLM cost by terminal outcome');
      expect(block).toHaveTextContent('$3.45 total');
      expect(block).toHaveTextContent('MERGED');
      expect(block).toHaveTextContent('$2.10');
      expect(block).toHaveTextContent('CODE_ERROR');
      expect(block).toHaveTextContent('$0.85');
      expect(block).toHaveTextContent('AGENT_TIMEOUT');
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

  it('renders llm cost decomposition (dominant model/task-type, rework, self-repair) when present', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: { cost_per_merged_pr: { current: 45.2, baseline: 50.0, drift_pct: -9.6, history: [] } },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
          llm_cost_attribution: {
            total_llm_usd: 5.00,
            by_failure_class: [
              { failure_class: 'MERGED', task_count: 10, total_cost_usd: 3.50 },
            ],
            by_model: [
              { provider: 'anthropic', model: 'claude-sonnet-4-6', cost_usd: 3.20 },
              { provider: 'anthropic', model: 'claude-haiku-4-5', cost_usd: 0.80 },
            ],
            by_task_type: [
              { task_type: 'create', cost_usd: 2.80 },
              { task_type: 'review', cost_usd: 1.40 },
            ],
            rework_share: 0.12,
            self_repair_burn_share: 0.05,
          },
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );
      await act(async () => { await Promise.resolve(); });

      const block = screen.getByTestId('llm-cost-by-outcome-block');
      // failure-class slice still present
      expect(block).toHaveTextContent('MERGED');
      expect(block).toHaveTextContent('$3.50');

      const decomp = screen.getByTestId('llm-cost-decomposition');
      // dominant model (claude-sonnet-4-6 has higher cost_usd)
      expect(decomp).toHaveTextContent('claude-sonnet-4-6');
      expect(decomp).toHaveTextContent('$3.20');
      // dominant task type
      expect(decomp).toHaveTextContent('create');
      expect(decomp).toHaveTextContent('$2.80');
      // rework_share 12%
      expect(decomp).toHaveTextContent('12%');
      // self_repair_burn_share 5%
      expect(decomp).toHaveTextContent('5%');
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

  it('renders failure-class slice and no crash when decomposition fields absent', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: { cost_per_merged_pr: { current: 10.0, baseline: 10.0, drift_pct: 0.0, history: [] } },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
          llm_cost_attribution: {
            total_llm_usd: 1.00,
            by_failure_class: [
              { failure_class: 'MERGED', task_count: 5, total_cost_usd: 1.00 },
            ],
            // by_model, by_task_type, rework_share, self_repair_burn_share absent
          },
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );
      await act(async () => { await Promise.resolve(); });

      // failure-class slice renders
      const block = screen.getByTestId('llm-cost-by-outcome-block');
      expect(block).toHaveTextContent('MERGED');
      // decomposition sub-block absent (no data to show)
      expect(screen.queryByTestId('llm-cost-decomposition')).toBeNull();
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

  it('omits the origin breakdown when the axes response lacks it', async () => {
    const original = apiFetch.getMockImplementation();
    if (!original) throw new Error('apiFetch mock implementation missing');
    apiFetch.mockImplementation(async (url: string) => {
      if (url.includes('/efficiency-axes')) {
        return new Response(JSON.stringify({
          axes: {
            vm_reuse_rate: { current: 72, baseline: 68, drift_pct: 5.9, history: [] },
          },
          baseline_captured_at: '2026-05-01T00:00:00Z',
          last_snapshot_at: '2026-05-08T00:00:00Z',
        }), { status: 200 });
      }
      return original(url);
    });

    try {
      render(
        <MetricsDashboard
          metricsHistory={[]}
          cumulativeCost={0}
          cumulativeVmHours={0}
          maxConcurrentVMs={4}
          tasks={[]}
          metricsWindow="6h"
          onWindowChange={vi.fn()}
          projectId="project-1"
        />,
      );

      await act(async () => {
        await Promise.resolve();
      });

      expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
    } finally {
      apiFetch.mockImplementation(original);
    }
  });

});
