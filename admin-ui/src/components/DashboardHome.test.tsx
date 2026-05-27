import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DashboardHome } from './DashboardHome';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

const { apiFetchMock, fetchQualificationVerdictsMock, capturedProps } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  fetchQualificationVerdictsMock: vi.fn().mockResolvedValue({ verdicts: [], total: 0 }),
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
  fetchQualificationVerdicts: (projectId: string, limit?: number) => fetchQualificationVerdictsMock(projectId, limit),
  fetchSrUnhandledEdgesRetrospectives: vi.fn().mockResolvedValue({ tasks: [] }),
  setVersionPhase: vi.fn(),
  triggerQualify: vi.fn(),
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
    status: 200,
    json: async () => data,
  } as Response;
}

function notFoundResponse(): Response {
  return {
    ok: false,
    status: 404,
    json: async () => ({}),
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
  directionSrBreakdown,
  promptFingerprintAnalytics,
}: {
  digest?: unknown;
  diagnostics?: unknown;
  openPRs?: unknown;
  mediumScope?: unknown;
  scopeBaselines?: unknown;
  accountHealth?: unknown;
  systemMaturity?: unknown;
  directionSrBreakdown?: unknown;
  promptFingerprintAnalytics?: unknown;
}) {
  apiFetchMock.mockImplementation((url: string) => {
    if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(digest ?? {}));
    if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(diagnostics ?? makeDiagnostics()));
    if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse(openPRs ?? { open_prs: [] }));
    if (url.includes('/api/admin/medium-scope-measurement')) return Promise.resolve(jsonResponse(mediumScope ?? {}));
    if (url.includes('/api/admin/scope-baselines')) return Promise.resolve(jsonResponse(scopeBaselines ?? {}));
    if (url.includes('/api/admin/max-plan-accounts/health')) return Promise.resolve(jsonResponse(accountHealth ?? { accounts: [] }));
    if (url.includes('/system-maturity')) return Promise.resolve(jsonResponse(systemMaturity ?? []));
    if (url.includes('/version-lifecycle')) return Promise.resolve(notFoundResponse());
    if (url.includes('/direction-sr-breakdown')) return Promise.resolve(directionSrBreakdown ? jsonResponse(directionSrBreakdown) : notFoundResponse());
    if (url.includes('/prompt-fingerprint-analytics')) return Promise.resolve(promptFingerprintAnalytics ? jsonResponse(promptFingerprintAnalytics) : notFoundResponse());
    if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
    // Unmatched endpoints (sr-blind-spots, sr-reviewer-disagreements,
    // sr-recent-misses, regression-cases, task-duration-stats, etc.) return 404 so the
    // component leaves the corresponding state slot at its initial null/[] sentinel rather
    // than ingesting an empty {} that would crash truthy-but-shapeless guards in JSX.
    return Promise.resolve(notFoundResponse());
  });
}

describe('DashboardHome', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    fetchQualificationVerdictsMock.mockReset();
    fetchQualificationVerdictsMock.mockResolvedValue({ verdicts: [], total: 0 });
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
    expect(screen.getByText('System Health')).toBeInTheDocument();
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

  it('calls onViewTask when clicking a task link in a health-overview issue', async () => {
    const linkedTask = makeTask({
      id: 'task-stuck',
      title: 'Repair failing deployment hook',
      status: 'IN_PROGRESS',
    });
    const onViewTask = vi.fn();

    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
      if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({
        overall_status: 'degraded',
        success_rate: { current: 0.72, trend: 'declining', window_hours: 24 },
        queue_depth: { queued: 1, active: 2, evaluating: 0 },
        actionable_issues: [
          {
            severity: 'warning',
            title: '1 task(s) lost heartbeat',
            hint: 'The reaper will reclaim and retry these tasks automatically.',
            task_ids: ['task-stuck'],
            category: 'stuck_tasks',
          },
        ],
      }));
      return Promise.resolve(notFoundResponse());
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[linkedTask]}
        selectedProjectId="proj1"
        onViewTask={onViewTask}
      />,
    );

    await user.click(await screen.findByRole('button', { name: /Repair failing deployment hook/i }));
    expect(onViewTask).toHaveBeenCalledWith(linkedTask);
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

  describe('SR Self-Fix Path card', () => {
    const baseSre = {
      with_self_review_approval_rate: 0.8,
      without_self_review_approval_rate: 0.6,
      clean_verdict_approval_rate: 0.75,
      fixed_verdict_approval_rate: 0.85,
    };

    it('renders self-fix and clean shares with per-bucket approval rates', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          self_review_effectiveness: {
            ...baseSre,
            sample_size: 120,
            sr_fixed_count: 22,
            sr_clean_count: 98,
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('SR Self-Fix Path')).toBeInTheDocument();
      // Existing SR Approval Lift tile keeps rendering.
      expect(screen.getByText('SR Approval Lift')).toBeInTheDocument();
      // Self-fix share = 22/120 = 18%; clean share = 98/120 = 82%.
      expect(screen.getByText('Self-fixed')).toBeInTheDocument();
      expect(screen.getByText(/18%/)).toBeInTheDocument();
      expect(screen.getByText(/n=22 of 120/)).toBeInTheDocument();
      expect(screen.getByText('Clean SR')).toBeInTheDocument();
      expect(screen.getByText(/82%/)).toBeInTheDocument();
      expect(screen.getByText(/n=98/)).toBeInTheDocument();
      // Per-bucket approval rates: 85% fixed, 75% clean.
      expect(screen.getByText(/85% approved/)).toBeInTheDocument();
      expect(screen.getByText(/75% approved/)).toBeInTheDocument();
    });

    it('does not render the card when sample_size is below the threshold', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          self_review_effectiveness: {
            ...baseSre,
            sample_size: 4,
            sr_fixed_count: 1,
            sr_clean_count: 3,
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      // SR Approval Lift still renders (its own threshold), but the new card does not.
      await screen.findByText('SR Approval Lift');
      expect(screen.queryByText('SR Self-Fix Path')).not.toBeInTheDocument();
    });

    it('does not render the card when count fields are missing', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          self_review_effectiveness: {
            ...baseSre,
            sample_size: 120,
            // sr_fixed_count and sr_clean_count omitted.
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('SR Approval Lift');
      expect(screen.queryByText('SR Self-Fix Path')).not.toBeInTheDocument();
    });
  });

  it('renders prompt fingerprint SR quality section when data is available', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      promptFingerprintAnalytics: {
        project_id: 'proj1',
        window_days: 30,
        sample_size: 150,
        project_first_attempt_success_rate: 0.88,
        project_sr_skip_rate: 0.05,
        fingerprints: [
          {
            fingerprint: 'abc123def456xyz789',
            sample_size: 80,
            first_attempt_measured: 70,
            first_attempt_success_rate: 0.9,
            sr_skip_rate: 0.04,
            rejection_category_counts: {},
            delta_vs_project_mean: 0.02,
            sr_sample_size: 65,
            sr_catch_rate: 0.72,
            sr_catch_sample_size: 40,
            reviewer_rescue_rate: 0.15,
            reviewer_rescue_sample_size: 30,
            sr_false_positive_rate: 0.08,
            sr_false_positive_sample_size: 25,
            sr_false_positive_rate_by_category: [],
            sr_reviewer_disagreement_top_categories: [
              { category: 'correctness', sample_size: 20, reviewer_overrode_sr_count: 3, sr_overrode_reviewer_count: 1 },
            ],
            sr_catch_rate_delta_vs_project_mean: 0.05,
          },
        ],
      },
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByTestId('prompt-fingerprint-sr-quality')).toBeInTheDocument();
    expect(screen.getByText('Prompt Fingerprint SR Quality')).toBeInTheDocument();
    // catch rate 72%
    expect(screen.getByText(/72%/)).toBeInTheDocument();
    // delta +5pp
    expect(screen.getByText(/\(\+5pp\)/)).toBeInTheDocument();
    // rescue rate 15%
    expect(screen.getByText(/15%/)).toBeInTheDocument();
    // disagreement category badge
    expect(screen.getByText('correctness')).toBeInTheDocument();
  });

  it('does not render prompt fingerprint SR quality section when no data', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    await screen.findByText('All systems healthy');
    expect(screen.queryByTestId('prompt-fingerprint-sr-quality')).not.toBeInTheDocument();
  });

  it('renders fix-iteration SR breakdown buckets when by_fix_iteration data is present', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
        self_review_prediction_accuracy_trends: {
          computed_at: '2026-05-01T00:00:00Z',
          categories: {},
          by_task_type: {},
          by_fix_iteration: {
            '1': { hit_rate: 0.8, miss_rate: 0.2, sample_count: 20 },
            '2': { hit_rate: 0.9, miss_rate: 0.1, sample_count: 10 },
            '3+': { hit_rate: 0.7, miss_rate: 0.3, sample_count: 3 },
          },
        },
      },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    // Buckets with sufficient signal show hit rate
    expect(await screen.findByText(/fix-1/)).toBeInTheDocument();
    expect(await screen.findByText(/fix-2/)).toBeInTheDocument();
    // fix-3+ has sample_count=3 (< 5), so it shows insufficient signal
    expect(await screen.findByText(/fix-3\+/)).toBeInTheDocument();
    expect(screen.getByTitle('insufficient signal (<5 qualifying tasks)')).toBeInTheDocument();
    // fix-1 hit rate: 80%
    expect(screen.getAllByText('80%').length).toBeGreaterThan(0);
  });

  describe('SR False Attestation tile', () => {
    it('renders false attestation tile with rate and per-category breakdown', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 10,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          sr_false_attestation_trends: {
            computed_at: '2026-05-08T00:00:00Z',
            bucket_mode: 'cycle',
            per_category: {
              tests_executed_cleanly: {
                false_attestation_rate: 0.12,
                previous_false_attestation_rate: 0.08,
                delta: 0.04,
                trend: 'regressing',
                ci_confirmed: 22,
                false_attested: 3,
                ci_data_missing: 1,
              },
              lint_executed_cleanly: {
                false_attestation_rate: 0.04,
                previous_false_attestation_rate: 0.05,
                delta: -0.01,
                trend: 'improving',
                ci_confirmed: 23,
                false_attested: 1,
                ci_data_missing: 0,
              },
            },
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      const tile = await screen.findByTestId('sr-false-attestation-tile');
      expect(tile).toBeInTheDocument();
      // Worst rate (12%) shown as headline
      expect(tile.textContent).toContain('12%');
      // Per-category rows for Tests and Lint
      expect(tile.textContent).toContain('Tests:');
      expect(tile.textContent).toContain('Lint:');
    });

    it('does not render false attestation tile when sr_false_attestation_trends is absent', async () => {
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

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      expect(screen.queryByTestId('sr-false-attestation-tile')).not.toBeInTheDocument();
    });

    it('does not render false attestation tile when all rates are null (missing CI data)', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          sr_false_attestation_trends: {
            computed_at: '2026-05-08T00:00:00Z',
            bucket_mode: 'cycle',
            per_category: {
              tests_executed_cleanly: {
                false_attestation_rate: null,
                ci_confirmed: 0,
                false_attested: 0,
                ci_data_missing: 10,
              },
            },
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      expect(screen.queryByTestId('sr-false-attestation-tile')).not.toBeInTheDocument();
    });
  });

  it('computes SR Catch Rate delta from overall hit rate, not average category delta', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 5,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
        self_review_prediction_accuracy: {
          prediction_hit_rate: 0.75,
          miss_rate: 0.25,
          false_alarm_rate: 0.1,
          sample_size: 20,
        },
        self_review_prediction_accuracy_trends: {
          overall_hit_rate: 0.75,
          previous_overall_hit_rate: 0.70,
          computed_at: '2026-05-20T00:00:00Z',
          categories: {
            correctness: {
              current_hit_rate: 0.80,
              previous_hit_rate: 0.60,
              current_human_count: 10,
              previous_human_count: 8,
            },
            type_safety: {
              current_hit_rate: 0.70,
              previous_hit_rate: 0.80,
              current_human_count: 5,
              previous_human_count: 6,
            },
            error_handling: {
              current_hit_rate: 0.75,
              previous_hit_rate: 0.75,
              current_human_count: 5,
              previous_human_count: 6,
            },
          },
        },
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

    const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
    expect(card).toBeInTheDocument();
    expect(card).toHaveTextContent('75%');
    expect(card).toHaveTextContent('+5pp');
    // n= shows reviewer finding count (10+5+5=20), not task count
    expect(card).toHaveTextContent('n=20 findings');
  });

  it('SR Catch Rate card shows too small when reviewer finding count is below threshold', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 5,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
        self_review_prediction_accuracy: {
          prediction_hit_rate: 0.75,
          miss_rate: 0.25,
          false_alarm_rate: 0.1,
          sample_size: 20,
        },
        self_review_prediction_accuracy_trends: {
          overall_hit_rate: 0.75,
          previous_overall_hit_rate: 0.70,
          computed_at: '2026-05-20T00:00:00Z',
          categories: {
            correctness: {
              current_hit_rate: 0.80,
              previous_hit_rate: 0.60,
              current_human_count: 2,
              previous_human_count: 8,
            },
            type_safety: {
              current_hit_rate: 0.70,
              previous_hit_rate: 0.80,
              current_human_count: 2,
              previous_human_count: 6,
            },
          },
        },
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

    const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
    expect(card).toBeInTheDocument();
    expect(card).toHaveTextContent('too small');
    // Shows finding count, not task count
    expect(card).toHaveTextContent('n=4 findings');
    expect(card).not.toHaveTextContent('75%');
  });

  describe('SR Catch Rate significance-gated verdict', () => {
    const baseDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      direction_progress: [],
      recent_completions: [],
      self_review_prediction_accuracy: { prediction_hit_rate: 0.75 },
    };
    // Enough findings (10 >= MIN_SR_CATCH_FINDINGS=5) so the card renders the full view
    const baseTrends = {
      overall_hit_rate: 0.75,
      computed_at: '2026-05-20T00:00:00Z',
      categories: {
        correctness: { current_hit_rate: 0.80, previous_hit_rate: 0.60, current_human_count: 10, previous_human_count: 8 },
      },
    };

    it('shows stable indicator (→) and opacity-50 when overall_trend_verdict is stable', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { ...baseTrends, previous_overall_hit_rate: 0.70, overall_trend_verdict: 'stable', overall_trend_significant: false } },
        diagnostics: makeDiagnostics(),
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
      expect(card).toHaveTextContent('75%');
      expect(card).toHaveTextContent('→');
      expect(card?.querySelector('.opacity-50')).not.toBeNull();
    });

    it('de-emphasizes delta with → when overall_trend_significant is false despite large raw delta', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { ...baseTrends, previous_overall_hit_rate: 0.60, overall_trend_verdict: null, overall_trend_significant: false } },
        diagnostics: makeDiagnostics(),
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
      expect(card).toHaveTextContent('+15pp');
      expect(card).toHaveTextContent('→');
      expect(card?.querySelector('.opacity-50')).not.toBeNull();
    });

    it('shows declining indicator (↓) when overall_trend_verdict is declining', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { ...baseTrends, previous_overall_hit_rate: 0.80, overall_trend_verdict: 'declining', overall_trend_significant: true } },
        diagnostics: makeDiagnostics(),
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
      expect(card).toHaveTextContent('↓');
      expect(card).toHaveTextContent('-5pp');
    });

    it('shows stale badge when trend data is stale', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { ...baseTrends, previous_overall_hit_rate: 0.70, is_stale: true } },
        diagnostics: makeDiagnostics(),
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
      expect(card).toHaveTextContent('stale');
      expect(card).not.toHaveTextContent('frozen');
    });

    it('shows frozen badge when data_sufficient is false and not stale', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { ...baseTrends, previous_overall_hit_rate: 0.70, is_stale: false, data_sufficient: false } },
        diagnostics: makeDiagnostics(),
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const card = await screen.findByText('SR Catch Rate').then((el) => el.closest('div[class*="rounded-lg"]'));
      expect(card).toHaveTextContent('frozen');
      expect(card).not.toHaveTextContent('stale');
    });
  });

  it('SR Unfixed Flags tile shows the self-flagged shipped-unfixed rate, not the catch hit rate', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 5,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
        self_review_prediction_accuracy: {
          prediction_hit_rate: 0.75,
          miss_rate: 0.25,
          false_alarm_rate: 0.1,
          sample_size: 20,
          self_flagged_shipped_unfixed_task_rate: 0.5,
          self_flagged_shipped_unfixed_task_count: 7,
        },
        // A high catch hit rate must NOT leak into the high=bad tile.
        self_review_prediction_accuracy_trends: {
          overall_hit_rate: 0.9,
          previous_overall_hit_rate: 0.85,
          computed_at: '2026-05-20T00:00:00Z',
          categories: {},
        },
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

    const tile = await screen.findByTestId('sr-self-flagged-unresolved-tile');
    // Rate comes from self_flagged_shipped_unfixed_task_rate (0.5), not overall_hit_rate (0.9).
    expect(tile).toHaveTextContent('50%');
    expect(tile).not.toHaveTextContent('90%');
    // Adjacent count comes from the same metric.
    expect(tile).toHaveTextContent('7 tasks');
    // High unfixed rate renders as red (high=bad).
    expect(tile.querySelector('.text-red-400')).not.toBeNull();
  });

  it('renders version lifecycle panel with current version and qualification details', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      if (url.includes('/version-lifecycle')) return Promise.resolve(jsonResponse({
        current_version: {
          version_number: 42,
          phase: 'stabilize',
          phase_changed_at: '2026-05-20T12:00:00Z',
          goal: 'Ship mobile-responsive admin UI',
          opened_at: '2026-05-15T00:00:00Z',
          qualification_attempts: 2,
          merged_task_count: 15,
          last_qualification: { status: 'dry_run', confidence: 0.78, threshold: 0.80, sub_scores: { audit: 0.85, test_erosion: 0.70, smoketest: 0.90 }, blocker: null, finalized_at: null },
        },
        recent_versions: [
          { version_number: 41, status: 'promoted', opened_at: '2026-05-10T00:00:00Z', promoted_at: '2026-05-15T00:00:00Z', qualification_attempts: 1, last_qualification: { status: 'promoted', confidence: 0.85, threshold: 0.80, sub_scores: null, blocker: null, finalized_at: null } },
        ],
      }));
      return Promise.resolve(notFoundResponse());
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    const panel = await screen.findByTestId('version-lifecycle-panel');
    expect(panel).toHaveTextContent('Version Lifecycle');
    expect(panel).toHaveTextContent('v42');
    expect(panel).toHaveTextContent('stabilize');
    expect(panel).toHaveTextContent('15 tasks merged');
    expect(panel).toHaveTextContent('Goal: Ship mobile-responsive admin UI');
    expect(panel).toHaveTextContent('78%/80% dry-run');
    expect(panel).toHaveTextContent('audit 85%');
    expect(panel).toHaveTextContent('History');
    expect(panel).toHaveTextContent('v41');
    expect(panel).toHaveTextContent('promoted');
  });

  it('shows green CI badge and resolved SHA in verdict history', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      if (url.includes('/version-lifecycle')) return Promise.resolve(jsonResponse({
        current_version: {
          version_number: 43,
          phase: 'stabilize',
          phase_changed_at: '2026-05-22T00:00:00Z',
          opened_at: '2026-05-20T00:00:00Z',
          qualification_attempts: 1,
          merged_task_count: 5,
          last_qualification: { status: 'dry_run', confidence: 0.91, threshold: 0.90, sub_scores: { audit: 0.95, test_erosion: 0.88, smoketest: 0.92 }, ci_resolution: { state: 'green', resolved_sha: 'abc1234567', detail: 'CI ran on abc1234' }, blocker: null, finalized_at: null },
        },
        recent_versions: [],
      }));
      return Promise.resolve(notFoundResponse());
    });
    fetchQualificationVerdictsMock.mockResolvedValue({
      verdicts: [{
        id: 'v1',
        release_version_id: 'rv1',
        candidate_sha: 'abc1234567890',
        confidence: 0.91,
        sub_scores: { audit: 0.95, test_erosion: 0.88, smoketest: 0.92, ci_resolution: { state: 'green', resolved_sha: 'abc1234567', detail: 'CI ran on abc1234' } },
        blocker: null,
        would_promote: true,
        promotion_enabled: true,
        terminal_status: 'dry_run',
        threshold: 0.90,
        started_at: '2026-05-22T00:00:00Z',
        finalized_at: '2026-05-22T01:00:00Z',
        created_at: '2026-05-22T00:00:00Z',
      }],
      total: 1,
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    const panel = await screen.findByTestId('version-lifecycle-panel');
    // Current-version sub-scores CI badge
    expect(panel).toHaveTextContent('ci:green');
    expect(panel).toHaveTextContent('@abc1234');
    // Verdict history CI badge
    const history = panel.querySelector('[data-testid="verdict-history"]');
    expect(history).not.toBeNull();
    expect(history).toHaveTextContent('ci:green');
    expect(history).toHaveTextContent('@abc1234');
  });

  it('shows red CI badge in verdict history for failed CI resolution', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      if (url.includes('/version-lifecycle')) return Promise.resolve(jsonResponse({
        current_version: {
          version_number: 44,
          phase: 'stabilize',
          phase_changed_at: '2026-05-22T00:00:00Z',
          opened_at: '2026-05-20T00:00:00Z',
          qualification_attempts: 1,
          merged_task_count: 3,
          last_qualification: { status: 'not_promoted', confidence: 0.50, threshold: 0.90, sub_scores: null, blocker: 'ci_failed', finalized_at: null },
        },
        recent_versions: [],
      }));
      return Promise.resolve(notFoundResponse());
    });
    fetchQualificationVerdictsMock.mockResolvedValue({
      verdicts: [{
        id: 'v2',
        release_version_id: 'rv2',
        candidate_sha: 'def9876543210',
        confidence: 0.50,
        sub_scores: { audit: 0.80, test_erosion: 0.75, smoketest: 0.85, ci_resolution: { state: 'red', resolved_sha: 'def987654', detail: 'CI failed on def9876' } },
        blocker: 'ci_failed',
        would_promote: false,
        promotion_enabled: false,
        terminal_status: 'not_promoted',
        threshold: 0.90,
        started_at: '2026-05-22T00:00:00Z',
        finalized_at: '2026-05-22T01:00:00Z',
        created_at: '2026-05-22T00:00:00Z',
      }],
      total: 1,
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    const panel = await screen.findByTestId('version-lifecycle-panel');
    const history = panel.querySelector('[data-testid="verdict-history"]');
    expect(history).not.toBeNull();
    expect(history).toHaveTextContent('ci:red');
    expect(history).toHaveTextContent('@def9876');
  });

  it('renders pending CI badge without crashing when resolved_sha is null', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      if (url.includes('/version-lifecycle')) return Promise.resolve(jsonResponse({
        current_version: {
          version_number: 45,
          phase: 'stabilize',
          phase_changed_at: '2026-05-22T00:00:00Z',
          opened_at: '2026-05-20T00:00:00Z',
          qualification_attempts: 1,
          merged_task_count: 2,
          last_qualification: { status: 'not_promoted', confidence: 0.60, threshold: 0.90, sub_scores: { audit: 0.90, test_erosion: 0.80, smoketest: 0.85 }, ci_resolution: { state: 'pending', resolved_sha: null, detail: 'no candidate sha' }, blocker: null, finalized_at: null },
        },
        recent_versions: [],
      }));
      return Promise.resolve(notFoundResponse());
    });
    fetchQualificationVerdictsMock.mockResolvedValue({
      verdicts: [{
        id: 'v3',
        release_version_id: 'rv3',
        candidate_sha: 'aaa1112223330',
        confidence: 0.60,
        sub_scores: { audit: 0.90, test_erosion: 0.80, smoketest: 0.85, ci_resolution: { state: 'pending', resolved_sha: null, detail: 'check-runs in progress' } },
        blocker: null,
        would_promote: false,
        promotion_enabled: false,
        terminal_status: 'not_promoted',
        threshold: 0.90,
        started_at: '2026-05-22T00:00:00Z',
        finalized_at: '2026-05-22T01:00:00Z',
        created_at: '2026-05-22T00:00:00Z',
      }],
      total: 1,
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    const panel = await screen.findByTestId('version-lifecycle-panel');
    expect(panel).toHaveTextContent('ci:pending');
    const history = panel.querySelector('[data-testid="verdict-history"]');
    expect(history).not.toBeNull();
    expect(history).toHaveTextContent('ci:pending');
    // null resolved_sha must not render an "@" sha segment for the CI badge
    expect(panel).not.toHaveTextContent('ci:pending @');
  });

  it('does not render version lifecycle panel when project has no lifecycle data', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });
    // /version-lifecycle returns 404 via setupApiMocks default

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    await screen.findByText('All systems healthy');
    expect(screen.queryByTestId('version-lifecycle-panel')).not.toBeInTheDocument();
  });
});
