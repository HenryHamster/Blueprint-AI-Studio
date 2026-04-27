import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DashboardHome } from './DashboardHome';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

vi.mock('./RecentErrorsWidget', () => ({
  RecentErrorsWidget: () => <div data-testid="recent-errors-widget">RecentErrorsWidget</div>,
}));

vi.mock('./DirectionAutoPauseWidget', () => ({
  DirectionAutoPauseWidget: () => <div data-testid="direction-auto-pause-widget">DirectionAutoPauseWidget</div>,
}));

vi.mock('./PRMonitorSection', () => ({
  PRMonitorSection: () => <div data-testid="pr-monitor-section">PRMonitorSection</div>,
}));

vi.mock('./MediumScopeWidget', () => ({
  MediumScopeWidget: () => <div data-testid="medium-scope-widget">MediumScopeWidget</div>,
}));

vi.mock('./RemediationFunnelSection', () => ({
  RemediationFunnelSection: () => <div data-testid="remediation-funnel-section">RemediationFunnelSection</div>,
}));

vi.mock('./FailureStageWidget', () => ({
  FailureStageWidget: () => <div data-testid="failure-stage-widget">FailureStageWidget</div>,
}));

vi.mock('./ScopeConversionRatesSection', () => ({
  ScopeConversionRatesSection: () => <div data-testid="scope-conversion-rates-section">ScopeConversionRatesSection</div>,
}));

vi.mock('./ScopeTrendsSection', () => ({
  ScopeTrendsSection: () => <div data-testid="scope-trends-section">ScopeTrendsSection</div>,
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
}));

function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    json: async () => data,
  } as Response;
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Task 1',
    description: 'Task description',
    status: 'QUEUED',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-04-01T00:00:00Z',
    ...overrides,
  };
}

function makeDiagnostics(overrides: Partial<ProjectDiagnostics> = {}): ProjectDiagnostics {
  return {
    project_id: 'proj1',
    stuck_tasks: [],
    circuit_breaker: { state: 'CLOSED', failure_count: 0, opened_at: null },
    queued_count: 0,
    in_progress_count: 0,
    zone_health: { available: 2, exhausted: [] },
    ...overrides,
  };
}

function setupApiMocks({
  digest,
  diagnostics,
  openPRs,
  mediumScope,
  scopeBaselines,
  accountHealth,
}: {
  digest?: unknown;
  diagnostics?: unknown;
  openPRs?: unknown;
  mediumScope?: unknown;
  scopeBaselines?: unknown;
  accountHealth?: unknown;
}) {
  apiFetchMock.mockImplementation((url: string) => {
    if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(digest ?? {}));
    if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(diagnostics ?? makeDiagnostics()));
    if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse(openPRs ?? { open_prs: [] }));
    if (url.includes('/api/admin/medium-scope-measurement')) return Promise.resolve(jsonResponse(mediumScope ?? {}));
    if (url.includes('/api/admin/scope-baselines')) return Promise.resolve(jsonResponse(scopeBaselines ?? {}));
    if (url.includes('/api/admin/max-plan-accounts/health')) return Promise.resolve(jsonResponse(accountHealth ?? { accounts: [] }));
    return Promise.resolve(jsonResponse({}));
  });
}

describe('DashboardHome', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows loading skeletons while digest is still loading', () => {
    apiFetchMock.mockImplementation(() => new Promise(() => {}));

    const { container } = render(
      <DashboardHome
        tasks={[]}
        selectedProjectId={null}
        onViewTask={vi.fn()}
      />,
    );

    expect(screen.getByText('Task Status')).toBeInTheDocument();
    expect(screen.getByText('Actionable Issues')).toBeInTheDocument();
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
    expect(screen.queryByText('All systems healthy')).not.toBeInTheDocument();
  });

  it('renders empty steady-state placeholders and zero values', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics(),
    });

    render(
      <DashboardHome
        tasks={[]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
      />,
    );

    expect(await screen.findByText('No active directions')).toBeInTheDocument();
    expect(screen.getByText('All systems healthy')).toBeInTheDocument();
    expect(screen.getByText('No recent merges')).toBeInTheDocument();
    expect(screen.getByText('No recent activity')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getAllByText('0').length).toBeGreaterThan(2);
  });

  it('renders populated digest metrics, failure pills, and latest cycle text', async () => {
    setupApiMocks({
      digest: {
        failed_count: 3,
        pending_approval_count: 1,
        completed_count: 7,
        success_rate: 0.75,
        trend: 'improving',
        pipeline_conversion_rate: 0.5,
        pipeline_acceptance_rate: 0.67,
        total_estimated_cost_usd: 12.3,
        narrative_summary: 'Pipeline stabilized after remediation work.',
        failure_groups: [
          {
            failure_class: 'CODE_ERROR',
            label: 'CODE ERROR',
            count: 2,
            tasks: [{ task_id: 'task-1', title: 'Fix parser crash' }],
          },
          {
            failure_class: 'AGENT_TIMEOUT',
            label: 'AGENT TIMEOUT',
            count: 1,
            tasks: [{ task_id: 'task-2', title: 'Long-running migration' }],
          },
        ],
        direction_progress: [
          {
            direction_id: 'dir-1',
            direction_text: 'Improve retry handling in CI path',
            priority: 'high',
            cycles_in_window: 2,
            tasks_created: 5,
            prs_merged: 2,
            possibly_saturated: false,
          },
        ],
        recent_completions: [
          {
            task_id: 'merge-1',
            title: 'Ship retry backoff',
            completed_at: '2026-04-24T01:00:00Z',
            pr_url: 'https://example.com/pr/1',
            task_type: 'fix',
          },
        ],
        latest_cycle: {
          cycle_id: 'c-1',
          started_at: '2026-04-24T00:00:00Z',
          completed_at: '2026-04-24T01:00:00Z',
          outcome: 'completed',
          directions_created: 2,
          directions_retired: 1,
          vision_updated: false,
          summary_text: 'Cycle completed successfully.',
        },
        trend_history: [
          {
            period_end: '2026-04-23T22:00:00Z',
            success_rate: 0.55,
            completed: 4,
            failed: 3,
            failed_by_class: { CODE_ERROR: 2, AGENT_TIMEOUT: 1 },
            period_cost_usd: 4,
          },
          {
            period_end: '2026-04-23T23:00:00Z',
            success_rate: 0.65,
            completed: 6,
            failed: 2,
            failed_by_class: { CODE_ERROR: 1, AGENT_TIMEOUT: 1 },
            period_cost_usd: 5,
          },
          {
            period_end: '2026-04-24T00:00:00Z',
            success_rate: 0.75,
            completed: 7,
            failed: 1,
            failed_by_class: { CODE_ERROR: 1, AGENT_TIMEOUT: 0 },
            period_cost_usd: 3.3,
          },
        ],
      },
      diagnostics: makeDiagnostics(),
    });

    render(
      <DashboardHome
        tasks={[
          makeTask({ id: 'task-queued', status: 'QUEUED' }),
          makeTask({ id: 'task-active', status: 'IN_PROGRESS' }),
        ]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
      />,
    );

    expect(await screen.findByText('CODE ERROR')).toBeInTheDocument();
    expect(screen.getByText('AGENT TIMEOUT')).toBeInTheDocument();
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText('Pipeline stabilized after remediation work.')).toBeInTheDocument();
    expect(screen.getByText(/Last cycle:/)).toBeInTheDocument();
    expect(screen.getByText(/2 created, 1 retired/)).toBeInTheDocument();
    expect(screen.getByText('Improve retry handling in CI path')).toBeInTheDocument();
  });

  it('calls onDirectionClick when a direction card is clicked', async () => {
    const onDirectionClick = vi.fn();

    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [
          {
            direction_id: 'dir-123',
            direction_text: 'Stabilize post-merge checks',
            priority: 'medium',
            cycles_in_window: 1,
            tasks_created: 2,
            prs_merged: 1,
            possibly_saturated: false,
          },
        ],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics(),
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
        onDirectionClick={onDirectionClick}
      />,
    );

    await user.click(await screen.findByText('Stabilize post-merge checks'));
    expect(onDirectionClick).toHaveBeenCalledWith('dir-123');
  });

  it('renders pool account health panel with a healthy account', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      accountHealth: {
        accounts: [
          {
            account_id: 'acct-1',
            label: 'Main Anthropic',
            provider: 'anthropic',
            state: 'healthy',
            health_score: 0.92,
            completions_24h: 120,
            rl_events_24h: 2,
            rate_limited_models: [],
            failure_class_counts: { CODE_ERROR: 3, RATE_LIMIT: 1 },
          },
        ],
      },
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByText('Provider Account Health')).toBeInTheDocument();
    expect(screen.getByText('Main Anthropic')).toBeInTheDocument();
    expect(screen.getByText('healthy')).toBeInTheDocument();
    expect(screen.getByText('92%')).toBeInTheDocument();
    expect(screen.getByText('120c 2rl')).toBeInTheDocument();
  });

  it('shows disabled state with reason and expands to reveal quota-vs-cooldown badges', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      accountHealth: {
        accounts: [
          {
            account_id: 'acct-2',
            label: 'Backup Account',
            provider: 'anthropic',
            state: 'disqualified',
            disabled_reason: null,
            health_score: 0.2,
            completions_24h: 5,
            rl_events_24h: 20,
            rate_limited_models: [
              { model: 'claude-3-5-sonnet', cooldown_seconds_remaining: 1800, cooldown_type: 'rate_limit' },
              { model: 'claude-3-opus', cooldown_seconds_remaining: 86400, cooldown_type: 'quota_exhausted' },
            ],
            failure_class_counts: { RATE_LIMIT: 20 },
          },
        ],
      },
    });

    const user = userEvent.setup();
    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByText('Provider Account Health')).toBeInTheDocument();
    expect(screen.getByText('disqualified')).toBeInTheDocument();

    // Expand the account row to reveal disabled-model badges
    await user.click(screen.getByRole('button', { name: /Backup Account/i }));

    expect(screen.getByText(/24h quota/)).toBeInTheDocument();
    expect(screen.getByText(/30m/)).toBeInTheDocument();
  });

  it('does not render pool health panel when no project is selected', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
    });

    render(<DashboardHome tasks={[]} selectedProjectId={null} onViewTask={vi.fn()} />);

    await screen.findByText('No active directions');
    expect(screen.queryByText('Provider Account Health')).not.toBeInTheDocument();
  });

  it('calls onViewTask when clicking a stuck-task actionable issue', async () => {
    const stuckTask = makeTask({
      id: 'task-stuck',
      title: 'Repair failing deployment hook',
      status: 'IN_PROGRESS',
    });
    const onViewTask = vi.fn();

    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics({
        stuck_tasks: [
          {
            task_id: 'task-stuck',
            title: 'Repair failing deployment hook',
            status: 'IN_PROGRESS',
            stuck_reason: 'no_heartbeat',
            stuck_for_seconds: 7200,
            attempt_count: 2,
            failure_class: null,
            unresolvable_deps: [],
            remediation_hint: 'Check VM logs',
          },
        ],
      }),
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[stuckTask]}
        selectedProjectId="proj1"
        onViewTask={onViewTask}
      />,
    );

    await user.click(await screen.findByText('Stuck: Repair failing deployment hook'));
    expect(onViewTask).toHaveBeenCalledWith(stuckTask);
  });
});
