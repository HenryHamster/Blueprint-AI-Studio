import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DashboardHome } from './DashboardHome';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

const { apiFetchMock, capturedProps } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  capturedProps: {
    prMonitor: null as Record<string, unknown> | null,
    remediationFunnel: null as Record<string, unknown> | null,
    providerCostPanel: null as Record<string, unknown> | null,
    scopeTrends: null as Record<string, unknown> | null,
    directionAutoPause: null as Record<string, unknown> | null,
  },
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

vi.mock('./RecentErrorsWidget', () => ({
  RecentErrorsWidget: () => <div data-testid="recent-errors-widget">RecentErrorsWidget</div>,
}));

vi.mock('./TaskDurationWidget', () => ({
  TaskDurationWidget: () => <div data-testid="task-duration-widget">TaskDurationWidget</div>,
}));

vi.mock('./DirectionAutoPauseWidget', () => ({
  DirectionAutoPauseWidget: (props: Record<string, unknown>) => {
    capturedProps.directionAutoPause = props;
    return <div data-testid="direction-auto-pause-widget">DirectionAutoPauseWidget</div>;
  },
}));

vi.mock('./PRMonitorSection', () => ({
  PRMonitorSection: (props: Record<string, unknown>) => {
    capturedProps.prMonitor = props;
    return <div data-testid="pr-monitor-section">PRMonitorSection</div>;
  },
}));

vi.mock('./MediumScopeWidget', () => ({
  MediumScopeWidget: () => <div data-testid="medium-scope-widget">MediumScopeWidget</div>,
}));

vi.mock('./RemediationFunnelSection', () => ({
  RemediationFunnelSection: (props: Record<string, unknown>) => {
    capturedProps.remediationFunnel = props;
    return <div data-testid="remediation-funnel-section">RemediationFunnelSection</div>;
  },
}));

vi.mock('./FailureStageWidget', () => ({
  FailureStageWidget: () => <div data-testid="failure-stage-widget">FailureStageWidget</div>,
}));

vi.mock('./ScopeConversionRatesSection', () => ({
  ScopeConversionRatesSection: () => <div data-testid="scope-conversion-rates-section">ScopeConversionRatesSection</div>,
}));

vi.mock('./ScopeTrendsSection', () => ({
  ScopeTrendsSection: (props: Record<string, unknown>) => {
    capturedProps.scopeTrends = props;
    return <div data-testid="scope-trends-section">ScopeTrendsSection</div>;
  },
}));

vi.mock('./ProviderCostPanel', () => ({
  ProviderCostPanel: (props: Record<string, unknown>) => {
    capturedProps.providerCostPanel = props;
    return <div data-testid="provider-cost-panel">ProviderCostPanel</div>;
  },
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
}));

vi.mock('./StrategicAuditPanel', () => ({
  StrategicAuditPanel: () => null,
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
  systemMaturity,
}: {
  digest?: unknown;
  diagnostics?: unknown;
  openPRs?: unknown;
  mediumScope?: unknown;
  scopeBaselines?: unknown;
  accountHealth?: unknown;
  systemMaturity?: unknown;
}) {
  apiFetchMock.mockImplementation((url: string) => {
    if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(digest ?? {}));
    if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(diagnostics ?? makeDiagnostics()));
    if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse(openPRs ?? { open_prs: [] }));
    if (url.includes('/api/admin/medium-scope-measurement')) return Promise.resolve(jsonResponse(mediumScope ?? {}));
    if (url.includes('/api/admin/scope-baselines')) return Promise.resolve(jsonResponse(scopeBaselines ?? {}));
    if (url.includes('/api/admin/max-plan-accounts/health')) return Promise.resolve(jsonResponse(accountHealth ?? { accounts: [] }));
    if (url.includes('/system-maturity')) return Promise.resolve(jsonResponse(systemMaturity ?? []));
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
        remediation_funnel: {
          residual_remediation_funnel: {
            open_cases_by_error_class: [
              {
                error_class: 'CODE_ERROR',
                count: 2,
                most_recent_at: '2026-04-24T00:30:00Z',
                cases: [
                  {
                    fingerprint: 'fp-1234567890abcdef',
                    status: 'ACTIVE',
                    hit_count: 4,
                    last_seen_at: '2026-04-24T00:30:00Z',
                    sample_message: 'gemini cli auth failed',
                  },
                ],
              },
            ],
          },
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
    expect(screen.getByText('Open Residual Cases')).toBeInTheDocument();
    expect(screen.getByText('CODE_ERROR · 2')).toBeInTheDocument();
    expect(screen.getByText(/ACTIVE · 4 hits/)).toBeInTheDocument();
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

  describe('CPM retirement-risk indicator', () => {
    const baseDirection = {
      direction_id: 'dir-cpm',
      direction_text: 'Expensive direction',
      priority: 'medium',
      cycles_in_window: 2,
      tasks_created: 5,
      prs_merged: 2,
      possibly_saturated: false,
    };

    it('shows warning indicator when cost_per_merge_exceeds_threshold is true', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          recent_completions: [],
          median_cost_per_merged_pr: 3.50,
          direction_progress: [
            { ...baseDirection, cost_per_merged_pr: 12.75, cost_per_merge_exceeds_threshold: true },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      const pill = await screen.findByTitle(/Cost per merged PR exceeds threshold/);
      expect(pill).toBeInTheDocument();
      expect(pill.textContent).toContain('$12.75/PR');
      expect(pill.title).toContain('project median: $3.50');
    });

    it('does not show warning indicator when cost_per_merge_exceeds_threshold is false', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          recent_completions: [],
          median_cost_per_merged_pr: 3.50,
          direction_progress: [
            { ...baseDirection, cost_per_merged_pr: 2.10, cost_per_merge_exceeds_threshold: false },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText(/\$2\.10\/PR/);
      expect(screen.queryByTitle(/Cost per merged PR exceeds threshold/)).not.toBeInTheDocument();
    });
  });

  it('renders SystemMaturityCard with rows including score and mode pill', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      systemMaturity: [
        {
          system_id: 'ci-pipeline',
          score: 35,
          recommended_mode: 'audit',
          top_issues: [{ description: 'High failure rate in test suite' }],
          last_audited_at: '2026-04-27T12:00:00Z',
        },
        {
          system_id: 'auth-service',
          score: 82,
          recommended_mode: 'ship',
          top_issues: [],
          last_audited_at: '2026-04-27T11:00:00Z',
        },
      ],
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(screen.getByText('System Maturity')).toBeInTheDocument();
    expect(await screen.findByText('ci-pipeline')).toBeInTheDocument();
    expect(screen.getByText('auth-service')).toBeInTheDocument();
    expect(screen.getByText('35')).toBeInTheDocument();
    expect(screen.getByText('82')).toBeInTheDocument();
    expect(screen.getByText('audit')).toBeInTheDocument();
    expect(screen.getByText('ship')).toBeInTheDocument();
  });

  it('renders System Maturity empty-state when no rows exist', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      systemMaturity: [],
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(screen.getByText('System Maturity')).toBeInTheDocument();
    expect(await screen.findByText('No audits have run yet')).toBeInTheDocument();
  });

  describe('project-level autonomous/assisted split', () => {
    const baseDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      recent_completions: [],
    };

    it('shows autonomous and assisted counts when attribution fields present', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            { direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 3, prs_merged: 5, possibly_saturated: false, autonomous_prs_merged: 4, assisted_prs_merged: 1 },
            { direction_id: 'd2', direction_text: 'Dir B', priority: 'low', cycles_in_window: 1, tasks_created: 2, prs_merged: 3, possibly_saturated: false, autonomous_prs_merged: 2, assisted_prs_merged: 1 },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('6 autonomous')).toBeInTheDocument();
      expect(screen.getByText('2 assisted')).toBeInTheDocument();
    });

    it('does not show split when attribution fields absent', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            { direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 3, prs_merged: 5, possibly_saturated: false },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('Dir A');
      expect(screen.queryByText(/autonomous/)).not.toBeInTheDocument();
      expect(screen.queryByText(/assisted/)).not.toBeInTheDocument();
    });

    it('shows only autonomous when all merges are autonomous', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            { direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 3, prs_merged: 7, possibly_saturated: false, autonomous_prs_merged: 7, assisted_prs_merged: 0 },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('7 autonomous')).toBeInTheDocument();
      expect(screen.getByText('0 assisted')).toBeInTheDocument();
    });
  });

  describe('widget prop wiring', () => {
    beforeEach(() => {
      capturedProps.prMonitor = null;
      capturedProps.remediationFunnel = null;
      capturedProps.providerCostPanel = null;
      capturedProps.scopeTrends = null;
      capturedProps.directionAutoPause = null;
    });

    const baseDigest = { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] };

    it('passes hasProject=false and tasks/onViewTask to PRMonitorSection when no project selected', async () => {
      setupApiMocks({ digest: baseDigest });
      const onViewTask = vi.fn();
      const tasks = [makeTask({ id: 'task-1' })];

      render(<DashboardHome tasks={tasks} selectedProjectId={null} onViewTask={onViewTask} />);

      await screen.findByText('No active directions');
      expect(capturedProps.prMonitor).not.toBeNull();
      const prProps = capturedProps.prMonitor ?? {};
      expect(prProps.hasProject).toBe(false);
      expect(prProps.tasks).toBe(tasks);
      expect(prProps.onViewTask).toBe(onViewTask);
    });

    it('passes hasProject=true and loading=false to PRMonitorSection when project is set and data loaded', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      const prProps = capturedProps.prMonitor ?? {};
      expect(prProps.hasProject).toBe(true);
      expect(prProps.loading).toBe(false);
    });

    it('passes projectId and loading to RemediationFunnelSection when project selected', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj-abc" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      const rfProps = capturedProps.remediationFunnel ?? {};
      expect(rfProps.projectId).toBe('proj-abc');
      expect(rfProps.loading).toBe(false);
    });

    it('passes selectedProjectId and windowHours=24 (default) to ProviderCostPanel', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      const cpProps = capturedProps.providerCostPanel ?? {};
      expect(cpProps.selectedProjectId).toBe('proj1');
      expect(cpProps.windowHours).toBe(24);
    });

    it('updates ProviderCostPanel windowHours when time-window selector changes', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });
      const user = userEvent.setup();

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      await user.selectOptions(screen.getByRole('combobox'), '48');

      const cpProps = capturedProps.providerCostPanel ?? {};
      expect(cpProps.windowHours).toBe(48);
    });

    it('passes open=false and onToggle to ScopeTrendsSection', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      const stProps = capturedProps.scopeTrends ?? {};
      expect(stProps.open).toBe(false);
      expect(typeof stProps.onToggle).toBe('function');
    });

    it('passes selectedProjectId to DirectionAutoPauseWidget', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj-xyz" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      const daProps = capturedProps.directionAutoPause ?? {};
      expect(daProps.selectedProjectId).toBe('proj-xyz');
    });
  });

  describe('self-sufficient directions roll-up', () => {
    const baseDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      recent_completions: [],
    };

    it('shows 0 / N when no directions meet threshold', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            { direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 2, prs_merged: 3, possibly_saturated: false },
            { direction_id: 'd2', direction_text: 'Dir B', priority: 'low', cycles_in_window: 1, tasks_created: 1, prs_merged: 1, possibly_saturated: false },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('Self-sufficient: 0 / 2')).toBeInTheDocument();
    });

    it('shows correct count when one direction is self-sufficient', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            {
              direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 2, prs_merged: 12, possibly_saturated: false,
              research_history: [{ autonomous_prs_merged: 12 }],
              history_aggregate: { total_autonomous_prs_merged: 0, total_operator_actions: 0 },
            },
            { direction_id: 'd2', direction_text: 'Dir B', priority: 'low', cycles_in_window: 1, tasks_created: 1, prs_merged: 2, possibly_saturated: false },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('Self-sufficient: 1 / 2')).toBeInTheDocument();
    });

    it('treats total_operator_actions undefined as null (not zero) when research_history has no attribution', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            {
              direction_id: 'd1', direction_text: 'Dir A', priority: 'high', cycles_in_window: 1, tasks_created: 2, prs_merged: 12, possibly_saturated: false,
              research_history: [{ autonomous_prs_merged: 12 }],
              history_aggregate: { total_autonomous_prs_merged: 0 },
            },
          ],
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      // total_operator_actions is undefined → null, so operatorActions === 0 is false → not self-sufficient
      expect(await screen.findByText('Self-sufficient: 0 / 1')).toBeInTheDocument();
    });
  });

  it('renders ManualInterventionPanel empty state when no escalations exist', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByTestId('manual-intervention-panel')).toBeInTheDocument();
    expect(screen.getByText('No regression cases need manual intervention')).toBeInTheDocument();
  });

  it('does not render ManualInterventionPanel when no project is selected', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
    });

    render(<DashboardHome tasks={[]} selectedProjectId={null} onViewTask={vi.fn()} />);

    await screen.findByText('No active directions');
    expect(screen.queryByTestId('manual-intervention-panel')).not.toBeInTheDocument();
  });

  it('does not render digest data when backend returns a response that fails the contract check', async () => {
    setupApiMocks({
      digest: { unexpected_field: 'bad', totally_wrong: true },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    // Diagnostics loads successfully so the component exits loading state
    expect(await screen.findByText('All systems healthy')).toBeInTheDocument();
    // Digest-specific failure pills and success rate must not appear
    expect(screen.queryByText('CODE ERROR')).not.toBeInTheDocument();
    expect(screen.queryByText('75%')).not.toBeInTheDocument();
    expect(screen.queryByText('Pipeline stabilized')).not.toBeInTheDocument();
  });
});
