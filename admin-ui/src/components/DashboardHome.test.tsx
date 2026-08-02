import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DashboardHome } from './DashboardHome';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

const { apiFetchMock, fetchQualificationVerdictsMock, fetchTaskMock, fetchFleetUtilizationMock, fetchAgentServingShasMock, setVersionPhaseMock, capturedProps } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  fetchQualificationVerdictsMock: vi.fn().mockResolvedValue({ verdicts: [], total: 0 }),
  fetchTaskMock: vi.fn(),
  fetchFleetUtilizationMock: vi.fn(),
  fetchAgentServingShasMock: vi.fn(),
  setVersionPhaseMock: vi.fn(),
  capturedProps: {
    prMonitor: null as Record<string, unknown> | null,
    remediationFunnel: null as Record<string, unknown> | null,
    providerCostPanel: null as Record<string, unknown> | null,
    scopeTrends: null as Record<string, unknown> | null,
    directionAutoPause: null as Record<string, unknown> | null,
    improvementLanes: null as Record<string, unknown> | null,
    srUnhandledEdges: null as Record<string, unknown> | null,
    srMissed: null as Record<string, unknown> | null,
    fixLoop: null as Record<string, unknown> | null,
    outerLoop: null as Record<string, unknown> | null,
  },
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
    fetchQualificationVerdicts: (projectId: string, limit?: number) => fetchQualificationVerdictsMock(projectId, limit),
    fetchTask: (taskId: string) => fetchTaskMock(taskId),
    fetchFleetUtilization: () => fetchFleetUtilizationMock(),
    fetchAgentServingShas: (projectId: string) => fetchAgentServingShasMock(projectId),
    fetchSrUnhandledEdgesRetrospectives: vi.fn().mockResolvedValue({ tasks: [] }),
    fetchSrCatchRateHealth: vi.fn().mockResolvedValue({
      status: 'healthy',
      last_sufficient_computed_at: null,
      last_sufficient_age_hours: null,
      signal_last_computed_at: null,
      signal_age_hours: null,
      data_sufficient: true,
    }),
    setVersionPhase: (projectId: string, phase: string, reasoning?: string) => setVersionPhaseMock(projectId, phase, reasoning),
    setVersionGoal: vi.fn(),
    triggerQualify: vi.fn(),
    // LlmCostBreakdownPanel (not mocked as a component) calls fetchLlmCostBreakdown.
    // Reject to simulate original behavior where the function was absent from the
    // narrow mock and calling it threw — causing the panel's try/catch to fire and
    // render nothing, avoiding a destructuring error on the undefined response.
    fetchLlmCostBreakdown: vi.fn().mockRejectedValue(new Error('not configured')),
  });
});

vi.mock('./RecentErrorsWidget', () => ({
  RecentErrorsWidget: () => <div data-testid="recent-errors-widget">RecentErrorsWidget</div>,
}));

vi.mock('./WarmReuseVsCostTrend', () => ({
  WarmReuseVsCostTrend: () => <div data-testid="warm-reuse-vs-cost-trend">WarmReuseVsCostTrend</div>,
}));

vi.mock('./ViewportDebtPanel', () => ({
  ViewportDebtPanel: ({ projectId }: { projectId: string }) => (
    <div data-testid="viewport-debt-panel" data-project-id={projectId}>ViewportDebtPanel</div>
  ),
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

vi.mock('./ImprovementLanesPanel', () => ({
  ImprovementLanesPanel: (props: Record<string, unknown>) => {
    capturedProps.improvementLanes = props;
    return <div data-testid="improvement-lanes-panel">ImprovementLanesPanel</div>;
  },
  default: (props: Record<string, unknown>) => {
    capturedProps.improvementLanes = props;
    return <div data-testid="improvement-lanes-panel">ImprovementLanesPanel</div>;
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

vi.mock('./SrUnhandledEdgesRetrospectivesPanel', () => ({
  SrUnhandledEdgesRetrospectivesPanel: (props: Record<string, unknown>) => {
    capturedProps.srUnhandledEdges = props;
    return <div data-testid="sr-unhandled-edges-panel">SrUnhandledEdgesRetrospectivesPanel</div>;
  },
}));

// Override only the two drill-through panels so we can capture their callbacks;
// keep every other export (SrTrendBreakdown, EdgesTrendBreakdown,
// DirectionSrBreakdownPanel) real so the "SR Catch Rate" card tests still render.
vi.mock('./SrTrendBreakdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./SrTrendBreakdown')>();
  return {
    ...actual,
    SrMissedRetrospective: (props: Record<string, unknown>) => {
      capturedProps.srMissed = props;
      return <div data-testid="sr-missed-retrospective">SrMissedRetrospective</div>;
    },
  };
});

vi.mock('./FixLoopConvergenceWidget', () => ({
  FixLoopConvergenceWidget: (props: Record<string, unknown>) => {
    capturedProps.fixLoop = props;
    return <div data-testid="fix-loop-convergence-widget">FixLoopConvergenceWidget</div>;
  },
}));

vi.mock('./OuterReviewLoopConvergenceWidget', () => ({
  OuterReviewLoopConvergenceWidget: (props: Record<string, unknown>) => {
    capturedProps.outerLoop = props;
    return <div data-testid="outer-loop-convergence-widget">OuterReviewLoopConvergenceWidget</div>;
  },
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

function errorResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => ({}),
  } as Response;
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>(r => {
    resolve = r;
  });
  return { promise, resolve };
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
  healthOverview,
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
  healthOverview?: unknown;
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
    if (url.includes('/health-overview')) return Promise.resolve(jsonResponse(healthOverview ?? { overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
    // Unmatched endpoints (sr-blind-spots, sr-recent-misses,
    // task-duration-stats, etc.) return 404 so the
    // component leaves the corresponding state slot at its initial null/[] sentinel rather
    // than ingesting an empty {} that would crash truthy-but-shapeless guards in JSX.
    return Promise.resolve(notFoundResponse());
  });
}

describe('DashboardHome', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    fetchQualificationVerdictsMock.mockReset();
    fetchTaskMock.mockReset();
    fetchFleetUtilizationMock.mockReset();
    fetchAgentServingShasMock.mockReset();
    setVersionPhaseMock.mockReset();
    fetchQualificationVerdictsMock.mockResolvedValue({ verdicts: [], total: 0 });
    setVersionPhaseMock.mockResolvedValue({
      version_number: 42,
      phase: 'build',
      phase_changed_at: '2026-05-20T12:05:00Z',
      phase_reasoning: 'operator advance',
    });
    fetchFleetUtilizationMock.mockResolvedValue({
      active_now: 0,
      nominal_capacity: null,
      usable_capacity: null,
      cooled_accounts: 0,
      util_now: null,
      util_avg: null,
      window_hours: 24,
      saturation: null,
    });
    fetchAgentServingShasMock.mockResolvedValue({
      fleet: { total_observed: 0, unknown_count: 0, mixed: false, buckets: [] },
      current_image: null,
      backend_served_sha: null,
      active_candidate_sha: null,
      integration_branch: 'automain',
      integration_head_sha: null,
      promotion_target: 'main',
      promotion_head_sha: null,
    });
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

  it('Active/Queued cards reflect aggregate counts, not the current task page slice', async () => {
    // The `tasks` prop is the current paginated page — here a single IN_PROGRESS
    // task. The membership-scoped aggregate props report the true fleet totals.
    // The headline cards must render the aggregate so a one-task page can't make
    // other projects' active/queued work disappear.
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
        tasks={[makeTask({ id: 'one-active', status: 'IN_PROGRESS' })]}
        selectedProjectId={null}
        globalActiveCount={12}
        globalQueuedCount={7}
        onViewTask={vi.fn()}
      />,
    );

    const section = (await screen.findByText('Task Status')).closest('section') as HTMLElement;
    const activeCard = (await within(section).findByText('Active')).closest('div') as HTMLElement;
    expect(within(activeCard).getByText('12')).toBeInTheDocument();
    // Not the page-slice value of 1.
    expect(within(activeCard).queryByText('1')).not.toBeInTheDocument();

    const queuedCard = within(section).getByText('Queued').closest('div') as HTMLElement;
    expect(within(queuedCard).getByText('7')).toBeInTheDocument();
  });

  it('Active/Queued cards prefer the project-scoped aggregate when a project is selected', async () => {
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
        globalActiveCount={12}
        globalQueuedCount={7}
        scopedActiveCount={3}
        scopedQueuedCount={2}
        onViewTask={vi.fn()}
      />,
    );

    const section = (await screen.findByText('Task Status')).closest('section') as HTMLElement;
    const activeCard = (await within(section).findByText('Active')).closest('div') as HTMLElement;
    // Project-scoped (3), not fleet (12).
    expect(within(activeCard).getByText('3')).toBeInTheDocument();
    const queuedCard = within(section).getByText('Queued').closest('div') as HTMLElement;
    expect(within(queuedCard).getByText('2')).toBeInTheDocument();
  });

  it('fetches project-scoped agent serving SHA data', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    await waitFor(() => {
      expect(fetchAgentServingShasMock).toHaveBeenCalledWith('proj1');
    });
  });

  it('mounts the improvement-lanes panel for a selected project', async () => {
    setupApiMocks({
      digest: {
        failed_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByTestId('improvement-lanes-panel')).toBeInTheDocument();
    expect(capturedProps.improvementLanes).toMatchObject({ projectId: 'proj1' });
  });

  it('keeps rendering when agent serving SHA fetch fails', async () => {
    fetchAgentServingShasMock.mockRejectedValueOnce(new Error('boom'));
    setupApiMocks({
      digest: {
        failed_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      },
      diagnostics: makeDiagnostics(),
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Task Status')).toBeInTheDocument();
    });
    expect(fetchAgentServingShasMock).toHaveBeenCalledWith('proj1');
  });

  it('renders directions needing attention and opens the directions view when clicked', async () => {
    const onDirectionClick = vi.fn();
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
      healthOverview: {
        overall_status: 'healthy',
        success_rate: { current: null, trend: null, window_hours: 24 },
        queue_depth: { queued: 0, active: 0, evaluating: 0 },
        actionable_issues: [],
        direction_portfolio: {
          possibly_complete_count: 2,
          missing_criteria_count: 1,
        },
      },
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

    const button = await screen.findByRole('button', { name: 'Open directions view' });
    expect(button).toHaveTextContent('2 possibly complete');
    expect(button).toHaveTextContent('1 missing criteria');
    await user.click(button);
    expect(onDirectionClick).toHaveBeenCalledWith('');
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

  it('threads response-level served_stale from the health endpoint into the pool panel', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
      accountHealth: {
        served_stale: true,
        generated_at: '2026-06-13T00:00:00Z',
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
            failure_class_counts: {},
          },
        ],
      },
    });

    render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

    expect(await screen.findByText('Provider Account Health')).toBeInTheDocument();
    // The response-level stale chip is present because served_stale flowed through.
    expect(screen.getByTitle(/whole panel is a cached fallback/i)).toBeInTheDocument();
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

      const pill = await screen.findByTitle(/Cost per autonomous merge exceeds threshold/);
      expect(pill).toBeInTheDocument();
      expect(pill.textContent).toContain('$12.75/autonomous');
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

      await screen.findByText(/\$2\.10\/autonomous/);
      expect(screen.queryByTitle(/Cost per autonomous merge exceeds threshold/)).not.toBeInTheDocument();
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
      capturedProps.srUnhandledEdges = null;
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

    it('passes selectedProjectId, trendDays, and windowHours through to the outer-loop widget', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });
      const user = userEvent.setup();

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      await user.selectOptions(screen.getByRole('combobox'), '48');
      await user.click(screen.getByRole('button', { name: '14d' }));

      const outerProps = capturedProps.outerLoop ?? {};
      expect(outerProps.projectId).toBe('proj1');
      expect(outerProps.windowHours).toBe(48);
      expect(outerProps.trendDays).toBe(14);
    });

    it('keeps stale project-scoped responses from overwriting the current dashboard context', async () => {
      const staleOpenPrs = deferred<Response>();

      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({ failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] }));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs?project_id=proj-a')) return staleOpenPrs.promise;
        if (url.includes('/api/admin/open-prs?project_id=proj-b')) {
          return Promise.resolve(jsonResponse({
            open_prs: [
              {
                pr_number: 202,
                pr_url: 'https://example.com/proj-b',
                pr_title: 'Project B PR',
                pr_branch: 'proj-b/feature',
                pr_base_branch: 'main',
                pr_created_at: '2026-05-01T00:00:00Z',
                tasks: [],
                is_orphaned: false,
              },
            ],
            project_integration_branch: 'main',
            project_promotion_target: 'main',
          }));
        }
        if (url.includes('/api/admin/medium-scope-measurement')) return Promise.resolve(jsonResponse({}));
        if (url.includes('/api/admin/scope-baselines')) return Promise.resolve(jsonResponse({}));
        if (url.includes('/api/admin/max-plan-accounts/health')) return Promise.resolve(jsonResponse({ accounts: [] }));
        if (url.includes('/api/admin/projects/') && url.includes('/system-maturity')) return Promise.resolve(jsonResponse([]));
        if (url.includes('/api/admin/projects/') && url.includes('/sr-blind-spots')) return Promise.resolve(notFoundResponse());
        if (url.includes('/api/admin/projects/') && url.includes('/sr-recent-misses')) return Promise.resolve(notFoundResponse());
        if (url.includes('/api/admin/projects/') && url.includes('/direction-sr-breakdown')) return Promise.resolve(notFoundResponse());
        if (url.includes('/api/admin/projects/') && url.includes('/prompt-fingerprint-analytics')) return Promise.resolve(notFoundResponse());
        if (url.includes('/api/admin/projects/') && url.includes('/version-lifecycle')) return Promise.resolve(notFoundResponse());
        if (url.includes('/api/admin/task-duration-stats')) return Promise.resolve(jsonResponse({}));
        if (url.includes('/api/admin/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      const user = userEvent.setup();
      const { rerender } = render(<DashboardHome tasks={[]} selectedProjectId="proj-a" onViewTask={vi.fn()} />);

      rerender(<DashboardHome tasks={[]} selectedProjectId="proj-b" onViewTask={vi.fn()} />);

      await user.selectOptions(screen.getByRole('combobox'), '48');
      await waitFor(() => {
        const prProps = capturedProps.prMonitor as { openPRs?: Array<{ pr_title: string }>; loading?: boolean } | null;
        expect(prProps?.loading).toBe(false);
        expect(prProps?.openPRs?.[0]?.pr_title).toBe('Project B PR');
      });

      staleOpenPrs.resolve(jsonResponse({
        open_prs: [
          {
            pr_number: 101,
            pr_url: 'https://example.com/proj-a',
            pr_title: 'Project A PR',
            pr_branch: 'proj-a/feature',
            pr_base_branch: 'main',
            pr_created_at: '2026-05-01T00:00:00Z',
            tasks: [],
            is_orphaned: false,
          },
        ],
        project_integration_branch: 'main',
        project_promotion_target: 'main',
      }));

      await waitFor(() => {
        const prProps = capturedProps.prMonitor as { openPRs?: Array<{ pr_title: string }> } | null;
        expect(prProps?.openPRs?.[0]?.pr_title).toBe('Project B PR');
        expect(prProps?.openPRs?.some(pr => pr.pr_title === 'Project A PR')).toBe(false);
      });
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

    it('passes selectedProjectId and onSelectTask to SrUnhandledEdgesRetrospectivesPanel', async () => {
      setupApiMocks({ digest: baseDigest, diagnostics: makeDiagnostics() });
      const onViewTask = vi.fn();
      const task = makeTask({ id: 'task-unhandled' });

      render(<DashboardHome tasks={[task]} selectedProjectId="proj-abc" onViewTask={onViewTask} />);

      await screen.findByText('All systems healthy');
      const ueProps = capturedProps.srUnhandledEdges ?? {};
      expect(ueProps.selectedProjectId).toBe('proj-abc');
      expect(typeof ueProps.onSelectTask).toBe('function');

      // onSelectTask should call onViewTask with the matching task
      (ueProps.onSelectTask as (id: string) => void)('task-unhandled');
      expect(onViewTask).toHaveBeenCalledWith(task);
    });

    it('drill-through shows dismissable error banner when fetchTask rejects', async () => {
      fetchTaskMock.mockRejectedValue(new Error('boom'));

      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(baseDigest));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      const onViewTask = vi.fn();
      const user = userEvent.setup();
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      await screen.findByText('All systems healthy');

      const ueProps = capturedProps.srUnhandledEdges ?? {};
      (ueProps.onSelectTask as (id: string) => void)('missing-task');

      const banner = await screen.findByTestId('sr-drill-through-error');
      expect(banner).toHaveTextContent('missing-task');
      expect(onViewTask).not.toHaveBeenCalled();

      // Dismiss the banner
      const dismiss = screen.getByRole('button', { name: /dismiss task load error/i });
      await user.click(dismiss);
      expect(screen.queryByTestId('sr-drill-through-error')).not.toBeInTheDocument();
    });
  });

  describe('SR drill-throughs resolve tasks by id (off-slice fetch fallback)', () => {
    const baseDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      direction_progress: [],
      recent_completions: [],
    };

    beforeEach(() => {
      capturedProps.srUnhandledEdges = null;
      capturedProps.srMissed = null;
      capturedProps.fixLoop = null;
    });

    // Populate the SR diagnostics endpoint so the Recent Misses section
    // renders and our mocked panel captures its drill-through callback.
    function setupSrApiMocks() {
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/sr-recent-misses')) {
          return Promise.resolve(jsonResponse({
            misses: [{ task_id: 'off-slice-miss', title: 'Off-slice miss', task_type: 'create', missed_categories: ['correctness'] }],
            total: 1,
          }));
        }
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(baseDigest));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse({ open_prs: [] }));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });
    }

    it('Recent SR Misses drill-through fetches an off-slice task by id and navigates', async () => {
      setupSrApiMocks();
      const onViewTask = vi.fn();
      const offSliceTask = makeTask({ id: 'off-slice-miss', title: 'Off-slice miss task' });
      fetchTaskMock.mockResolvedValue(offSliceTask);

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      await waitFor(() => expect(capturedProps.srMissed).not.toBeNull());
      const props = capturedProps.srMissed ?? {};
      expect(typeof props.onTaskClick).toBe('function');

      (props.onTaskClick as (id: string) => void)('off-slice-miss');

      await waitFor(() => expect(fetchTaskMock).toHaveBeenCalledWith('off-slice-miss'));
      await waitFor(() => expect(onViewTask).toHaveBeenCalledWith(offSliceTask));
    });

    it('SR Unhandled Edges drill-through fetches an off-slice task by id and navigates', async () => {
      setupSrApiMocks();
      const onViewTask = vi.fn();
      const offSliceTask = makeTask({ id: 'off-slice-edge', title: 'Off-slice edge task' });
      fetchTaskMock.mockResolvedValue(offSliceTask);

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      await waitFor(() => expect(capturedProps.srUnhandledEdges).not.toBeNull());
      const props = capturedProps.srUnhandledEdges ?? {};
      expect(typeof props.onSelectTask).toBe('function');

      (props.onSelectTask as (id: string) => void)('off-slice-edge');

      await waitFor(() => expect(fetchTaskMock).toHaveBeenCalledWith('off-slice-edge'));
      await waitFor(() => expect(onViewTask).toHaveBeenCalledWith(offSliceTask));
    });

    it('fix-loop convergence widget fetches an off-slice task by id and navigates', async () => {
      setupSrApiMocks();
      const onViewTask = vi.fn();
      const offSliceTask = makeTask({ id: 'off-slice-fix', title: 'Off-slice fix task' });
      fetchTaskMock.mockResolvedValue(offSliceTask);

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      await waitFor(() => expect(capturedProps.fixLoop).not.toBeNull());
      const props = capturedProps.fixLoop ?? {};
      expect(typeof props.onViewTask).toBe('function');

      (props.onViewTask as (id: string) => void)('off-slice-fix');

      await waitFor(() => expect(fetchTaskMock).toHaveBeenCalledWith('off-slice-fix'));
      await waitFor(() => expect(onViewTask).toHaveBeenCalledWith(offSliceTask));
    });

    it('review-quality catch-rate regression drilldown fetches an off-slice task by id and navigates', async () => {
      const onViewTask = vi.fn();
      const offSliceTask = makeTask({ id: 'off-slice-rq', title: 'Off-slice review-quality task' });
      fetchTaskMock.mockResolvedValue(offSliceTask);

      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/sr-alert-drilldown')) {
          return Promise.resolve(jsonResponse({
            items: [{ task_id: 'off-slice-rq', title: 'Off-slice review-quality task', missed_categories: ['correctness'], sr_verdict: 'pass' }],
          }));
        }
        if (url.includes('/sr-investigation-task-ids')) return Promise.resolve(jsonResponse({ task_ids: {} }));
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({
          ...baseDigest,
          self_review_prediction_accuracy_trends: {
            overall_hit_rate: 0.5,
            computed_at: '2026-05-20T00:00:00Z',
            categories: {
              correctness: { current_hit_rate: 0.4, previous_hit_rate: 0.7, current_human_count: 10, previous_human_count: 8, trend: 'decreasing' },
            },
          },
        }));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse({ open_prs: [] }));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      const user = userEvent.setup();
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      const investigateBtn = await screen.findByText(/Investigate regression/);
      await user.click(investigateBtn);

      const viewBtn = await screen.findByTitle('View task SR panel');
      await user.click(viewBtn);

      await waitFor(() => expect(fetchTaskMock).toHaveBeenCalledWith('off-slice-rq'));
      await waitFor(() => expect(onViewTask).toHaveBeenCalledWith(offSliceTask));
    });

    it('uses the in-memory task without fetching when it is in the loaded slice (sanity)', async () => {
      setupSrApiMocks();
      const onViewTask = vi.fn();
      const inSliceTask = makeTask({ id: 'in-slice-1', title: 'In-slice task' });

      render(<DashboardHome tasks={[inSliceTask]} selectedProjectId="proj1" onViewTask={onViewTask} />);

      await waitFor(() => expect(capturedProps.srMissed).not.toBeNull());
      (capturedProps.srMissed?.onTaskClick as (id: string) => void)('in-slice-1');

      expect(onViewTask).toHaveBeenCalledWith(inSliceTask);
      expect(fetchTaskMock).not.toHaveBeenCalled();
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

    it('prefers the authoritative direction_portfolio counts when present', async () => {
      setupApiMocks({
        digest: {
          ...baseDigest,
          direction_progress: [
            {
              direction_id: 'd1',
              direction_text: 'Dir A',
              priority: 'high',
              cycles_in_window: 1,
              tasks_created: 2,
              prs_merged: 12,
              possibly_saturated: false,
              research_history: [{ autonomous_prs_merged: 12 }],
              history_aggregate: { total_autonomous_prs_merged: 0, total_operator_actions: 0 },
            },
          ],
          direction_portfolio: {
            active_count: 3,
            self_sufficient_count: 2,
            composition: [
              { direction_id: 'd1', direction_text: 'Dir A', status: 'active', priority: 'high', quiet_this_window: false },
              { direction_id: 'd2', direction_text: 'Dir B', status: 'held', priority: 'low', quiet_this_window: true },
              { direction_id: 'd3', direction_text: 'Dir C', status: 'auto_paused', priority: 'medium', quiet_this_window: true },
            ],
          },
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('Self-sufficient: 2 / 3')).toBeInTheDocument();
      expect(screen.getByText('3 directions')).toBeInTheDocument();
      expect(screen.getByText('2 quiet this window')).toBeInTheDocument();
    });
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
            sr_false_positive_rate: 0.08,
            sr_false_positive_sample_size: 25,
            sr_false_positive_rate_by_category: [],
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

  describe('DirectionSrBreakdownPanel freshness pass-through', () => {
    const baseDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      direction_progress: [],
      recent_completions: [],
      self_review_prediction_accuracy: { prediction_hit_rate: 0.75 },
    };
    const directionSrBreakdown = {
      min_sample: 3,
      directions: [
        { direction_id: 'd1', direction_text: 'Dir A', sample_size: 10, overall_catch_rate: 0.5, worst_category: null, categories: {} },
      ],
    };

    it('renders the stale badge inside the direction breakdown section when the snapshot is stale', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { overall_hit_rate: 0.75, computed_at: '2026-05-20T00:00:00Z', is_stale: true, categories: {} } },
        diagnostics: makeDiagnostics(),
        directionSrBreakdown,
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const section = await screen.findByTestId('direction-sr-breakdown');
      expect(within(section).getByTestId('sr-trend-stale-badge')).toBeInTheDocument();
      expect(within(section).queryByTestId('sr-trend-insufficient-badge')).toBeNull();
    });

    it('renders the frozen badge inside the direction breakdown section when data is insufficient', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { overall_hit_rate: 0.75, computed_at: '2026-05-20T00:00:00Z', is_stale: false, data_sufficient: false, last_sufficient_computed_at: '2026-04-15T00:00:00Z', categories: {} } },
        diagnostics: makeDiagnostics(),
        directionSrBreakdown,
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const section = await screen.findByTestId('direction-sr-breakdown');
      const badge = within(section).getByTestId('sr-trend-insufficient-badge');
      expect(badge).toHaveTextContent(`frozen · last real data ${new Date('2026-04-15T00:00:00Z').toLocaleDateString()}`);
    });

    it('renders no freshness badge in the direction breakdown section when the snapshot is fresh and sufficient', async () => {
      setupApiMocks({
        digest: { ...baseDigest, self_review_prediction_accuracy_trends: { overall_hit_rate: 0.75, computed_at: '2026-05-20T00:00:00Z', is_stale: false, data_sufficient: true, categories: {} } },
        diagnostics: makeDiagnostics(),
        directionSrBreakdown,
      });
      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      const section = await screen.findByTestId('direction-sr-breakdown');
      expect(within(section).queryByTestId('sr-trend-stale-badge')).toBeNull();
      expect(within(section).queryByTestId('sr-trend-insufficient-badge')).toBeNull();
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

  it('renders SR Blind Spots section with expandable pills and examples', async () => {
    const mockTask = makeTask({ id: 'task-123', title: 'Example task' });
    fetchTaskMock.mockResolvedValue(mockTask);

    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/sr-blind-spots')) {
        return Promise.resolve(jsonResponse({
          themes: [
            {
              theme: 'did not run the full backend',
              count: 5,
              examples: [
                { text: 'did not run the full backend test suite', task_id: 'task-123', task_type: 'create' },
                { text: 'did not run the full backend integration tests', task_id: 'task-124', task_type: 'fix' },
                { text: 'did not run the full backend docker tests', task_id: 'task-125', task_type: 'create' },
              ],
              layer_distribution: { backend: 3, frontend: 2 },
            },
          ],
          total_distinct_themes: 1,
          window_days: 14,
        }));
      }
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({}));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      return Promise.resolve(notFoundResponse());
    });

    const { container } = render(
      <DashboardHome
        tasks={[mockTask]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
      />,
    );

    // Wait for the section to appear and verify the theme pill is visible
    expect(await screen.findByText('SR Blind Spots')).toBeInTheDocument();
    expect(screen.getByText('did not run the full backend')).toBeInTheDocument();
    expect(screen.getByText('×5')).toBeInTheDocument();
    void container; // Verify component rendered without error

    // Verify layer distribution is shown
    expect(screen.getByText('backend')).toBeInTheDocument();
    expect(screen.getByText('frontend')).toBeInTheDocument();

    // Examples should NOT be visible initially
    expect(screen.queryByText('did not run the full backend test suite')).not.toBeInTheDocument();
  });

  it('expands blind spot pill to show examples when clicked', async () => {
    const mockTask = makeTask({ id: 'task-123', title: 'Example task' });
    fetchTaskMock.mockResolvedValue(mockTask);

    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/sr-blind-spots')) {
        return Promise.resolve(jsonResponse({
          themes: [
            {
              theme: 'did not run the full backend',
              count: 3,
              examples: [
                { text: 'did not run the full backend test suite', task_id: 'task-123', task_type: 'create' },
                { text: 'did not run the full backend integration tests', task_id: 'task-124', task_type: 'fix' },
              ],
              layer_distribution: {},
            },
          ],
          total_distinct_themes: 1,
          window_days: 14,
        }));
      }
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({}));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      return Promise.resolve(notFoundResponse());
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[mockTask]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
      />,
    );

    // Wait for the blind spot to load
    const pill = await screen.findByRole('button', { name: /did not run the full backend/ });
    expect(pill).toBeInTheDocument();

    // Click the pill to expand
    await user.click(pill);

    // Examples should now be visible
    expect(screen.getByText('did not run the full backend test suite')).toBeInTheDocument();
    expect(screen.getByText('did not run the full backend integration tests')).toBeInTheDocument();
    expect(screen.getAllByText('View task').length).toBeGreaterThanOrEqual(2);
  });

  it('calls onViewTask when an example task link is clicked', async () => {
    const mockTask = makeTask({ id: 'task-123', title: 'Example task' });
    const mockTask124 = makeTask({ id: 'task-124', title: 'Another task' });
    const onViewTask = vi.fn();
    fetchTaskMock.mockResolvedValue(mockTask124);

    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/sr-blind-spots')) {
        return Promise.resolve(jsonResponse({
          themes: [
            {
              theme: 'did not run the full backend',
              count: 2,
              examples: [
                { text: 'did not run the full backend test suite', task_id: 'task-123', task_type: 'create' },
                { text: 'did not run the full backend integration tests', task_id: 'task-124', task_type: 'fix' },
              ],
              layer_distribution: {},
            },
          ],
          total_distinct_themes: 1,
          window_days: 14,
        }));
      }
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({}));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      return Promise.resolve(notFoundResponse());
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[mockTask, mockTask124]}
        selectedProjectId="proj1"
        onViewTask={onViewTask}
      />,
    );

    // Expand the pill
    const pill = await screen.findByRole('button', { name: /did not run the full backend/ });
    await user.click(pill);

    // Click the first "View task" link
    const viewTaskLinks = screen.getAllByText('View task');
    await user.click(viewTaskLinks[0]);

    // Should call onViewTask with the correct task (task-123)
    expect(onViewTask).toHaveBeenCalledWith(mockTask);
  });

  it('expands and collapses blind spot pills independently', async () => {
    setupApiMocks({
      digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
      diagnostics: makeDiagnostics(),
    });

    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/sr-blind-spots')) {
        return Promise.resolve(jsonResponse({
          themes: [
            {
              theme: 'did not run the full backend',
              count: 2,
              examples: [
                { text: 'backend example 1', task_id: 'task-1', task_type: 'create' },
              ],
              layer_distribution: {},
            },
            {
              theme: 'did not run the full frontend',
              count: 1,
              examples: [
                { text: 'frontend example 1', task_id: 'task-2', task_type: 'create' },
              ],
              layer_distribution: {},
            },
          ],
          total_distinct_themes: 2,
          window_days: 14,
        }));
      }
      if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse({}));
      if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
      return Promise.resolve(notFoundResponse());
    });

    const user = userEvent.setup();
    render(
      <DashboardHome
        tasks={[]}
        selectedProjectId="proj1"
        onViewTask={vi.fn()}
      />,
    );

    // Wait for both pills to appear
    const backendPill = await screen.findByRole('button', { name: /did not run the full backend/ });
    const frontendPill = screen.getByRole('button', { name: /did not run the full frontend/ });

    // Expand first pill
    await user.click(backendPill);
    expect(screen.getByText('backend example 1')).toBeInTheDocument();
    expect(screen.queryByText('frontend example 1')).not.toBeInTheDocument();

    // Expand second pill
    await user.click(frontendPill);
    expect(screen.getByText('frontend example 1')).toBeInTheDocument();
    expect(screen.getByText('backend example 1')).toBeInTheDocument();

    // Collapse first pill
    await user.click(backendPill);
    expect(screen.queryByText('backend example 1')).not.toBeInTheDocument();
    expect(screen.getByText('frontend example 1')).toBeInTheDocument();
  });

  describe('EffortEfficiencyPanel mount', () => {
    const efficiencyDigest = {
      failed_count: 0,
      pending_approval_count: 0,
      completed_count: 0,
      failure_groups: [],
      direction_progress: [],
      recent_completions: [],
      effort_model_efficiency: {
        by_effort: [
          {
            effort: 'high',
            run_count: 5,
            landed_count: 2,
            landed_rate: 0.4,
            total_cost_usd: 30,
            cost_per_merge: 15,
            input_tokens: 1000,
            output_tokens: 500,
            llm_call_count: 12,
            priced_run_count: 5,
            unpriced_run_count: 0,
            unpriced_coverage_fraction: 1,
            resolved_models: [],
          },
        ],
        by_model: [
          {
            provider: 'anthropic',
            model: 'claude-opus-4-8',
            priced: true,
            run_count: 5,
            landed_count: 2,
            landed_rate: 0.4,
            total_cost_usd: 30,
            cost_per_merge: 15,
            input_tokens: 1000,
            output_tokens: 500,
            llm_call_count: 12,
            priced_run_count: 5,
            unpriced_run_count: 0,
            unpriced_coverage_fraction: 1,
          },
        ],
        totals: {
          run_count: 5,
          landed_count: 2,
          landed_rate: 0.4,
          total_cost_usd: 30,
          cost_per_merge: 15,
          input_tokens: 1000,
          output_tokens: 500,
          llm_call_count: 12,
          priced_run_count: 5,
          unpriced_run_count: 0,
          unpriced_coverage_fraction: 1,
        },
        unpriced_combos: [],
        fallbacks: { total: 0, combos: [] },
      },
    };

    it('renders the panel when the digest carries the effort/model rollup', async () => {
      setupApiMocks({ digest: efficiencyDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByText('Effort & Model Efficiency')).toBeInTheDocument();
    });

    it('omits the panel when the digest has no effort/model rollup', async () => {
      setupApiMocks({
        digest: { failed_count: 0, pending_approval_count: 0, completed_count: 0, failure_groups: [], direction_progress: [], recent_completions: [] },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      expect(screen.queryByText('Effort & Model Efficiency')).not.toBeInTheDocument();
    });
  });

  describe('digest staleness banner', () => {
    it('surfaces partial dispatch gates from the runtime state', async () => {
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
          gatedTaskTypes={[{ task_type: 'create', failure_rate: 0.83, sample_size: 12, threshold: 0.6 }]}
        />,
      );

      expect(await screen.findByText(/Partial dispatch gate — 1 task type blocked/)).toBeInTheDocument();
      expect(screen.getByText('create')).toBeInTheDocument();
    });

    it('shows the stale banner when the backend returns served_stale=true', async () => {
      setupApiMocks({
        digest: {
          failed_count: 2,
          pending_approval_count: 0,
          completed_count: 5,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          served_stale: true,
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByTestId('digest-stale-banner')).toBeInTheDocument();
      // Should NOT show the "Backend unreachable" banner — we have data
      expect(screen.queryByText(/Backend unreachable/)).not.toBeInTheDocument();
    });

    it('shows the stale banner when digest fetch fails but previous data exists', async () => {
      const validDigest = {
        failed_count: 1,
        pending_approval_count: 0,
        completed_count: 3,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      };

      // First call: returns valid digest
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(validDigest));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse({ open_prs: [] }));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      // Wait for initial successful load
      await screen.findByText('All systems healthy');
      expect(screen.queryByTestId('digest-stale-banner')).not.toBeInTheDocument();

      // Second call: digest fails
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/api/admin/digest')) return Promise.resolve(errorResponse(503));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs')) return Promise.resolve(jsonResponse({ open_prs: [] }));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      // Trigger a manual re-render cycle via window change (simulates poller tick)
      const { rerender } = render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      rerender(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      // After fetch failure with prior data, stale banner should appear
      await waitFor(() => {
        expect(screen.queryByTestId('digest-stale-banner')).not.toBeNull();
      });
      // "Backend unreachable" banner should NOT appear since we have previous data
      expect(screen.queryByText(/Backend unreachable/)).not.toBeInTheDocument();
    });

    it('shows tasks-truncated banner when digest.tasks_truncated is true', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          tasks_truncated: true,
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      expect(await screen.findByTestId('tasks-truncated-banner')).toBeInTheDocument();
      expect(screen.getByTestId('tasks-truncated-banner')).toHaveTextContent(/partial view/);
    });

    it('does not show tasks-truncated banner when tasks_truncated is false', async () => {
      setupApiMocks({
        digest: {
          failed_count: 0,
          pending_approval_count: 0,
          completed_count: 0,
          failure_groups: [],
          direction_progress: [],
          recent_completions: [],
          tasks_truncated: false,
        },
        diagnostics: makeDiagnostics(),
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);

      await screen.findByText('All systems healthy');
      expect(screen.queryByTestId('tasks-truncated-banner')).not.toBeInTheDocument();
    });

    it('does not show tasks-truncated banner when tasks_truncated is absent', async () => {
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
      expect(screen.queryByTestId('tasks-truncated-banner')).not.toBeInTheDocument();
    });

    it('passes degraded=true to PRMonitorSection when open-prs payload is degraded', async () => {
      const validDigest = {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      };

      setupApiMocks({
        digest: validDigest,
        diagnostics: makeDiagnostics(),
        openPRs: {
          open_prs: [],
          project_integration_branch: 'main',
          project_promotion_target: 'main',
          degraded: true,
          degraded_reason: 'github_token_error',
        },
      });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      await screen.findByText('All systems healthy');

      expect(capturedProps.prMonitor?.stale).toBeFalsy();
      expect(capturedProps.prMonitor?.degraded).toBe(true);
    });

    it('passes stale=true to PRMonitorSection when open-prs fetch fails after prior success', async () => {
      const validDigest = {
        failed_count: 0,
        pending_approval_count: 0,
        completed_count: 0,
        failure_groups: [],
        direction_progress: [],
        recent_completions: [],
      };

      setupApiMocks({ digest: validDigest, diagnostics: makeDiagnostics() });

      render(<DashboardHome tasks={[]} selectedProjectId="proj1" onViewTask={vi.fn()} />);
      await screen.findByText('All systems healthy');

      // PRMonitorSection should receive stale=false initially
      expect(capturedProps.prMonitor?.stale).toBeFalsy();

      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('/api/admin/digest')) return Promise.resolve(jsonResponse(validDigest));
        if (url.includes('/diagnostics')) return Promise.resolve(jsonResponse(makeDiagnostics()));
        if (url.includes('/api/admin/open-prs')) return Promise.resolve(errorResponse(503));
        if (url.includes('/health-overview')) return Promise.resolve(jsonResponse({ overall_status: 'healthy', success_rate: { current: null, trend: null, window_hours: 24 }, queue_depth: { queued: 0, active: 0, evaluating: 0 }, actionable_issues: [] }));
        return Promise.resolve(notFoundResponse());
      });

      const refresh = capturedProps.prMonitor?.onRefresh as (() => Promise<void>) | undefined;
      expect(refresh).toBeDefined();
      if (refresh) {
        await refresh();
      }

      await waitFor(() => {
        expect(capturedProps.prMonitor?.stale).toBe(true);
      });
      expect(capturedProps.prMonitor?.degraded).toBeFalsy();
    });
  });

  describe('ViewportDebtPanel visibility', () => {
    it('hides ViewportDebtPanel for a non-self-repo project', async () => {
      setupApiMocks({});
      render(
        <DashboardHome
          tasks={[]}
          selectedProjectId="proj-other"
          projects={[{ id: 'proj-other', name: 'Other', is_self_repo: false } as import('../types/project').Project]}
          onViewTask={vi.fn()}
        />,
      );
      await screen.findByText('All systems healthy');
      expect(screen.queryByTestId('viewport-debt-panel')).not.toBeInTheDocument();
    });

    it('shows ViewportDebtPanel for a self-repo project', async () => {
      setupApiMocks({});
      render(
        <DashboardHome
          tasks={[]}
          selectedProjectId="proj-self"
          projects={[{ id: 'proj-self', name: 'Blueprint', is_self_repo: true } as import('../types/project').Project]}
          onViewTask={vi.fn()}
        />,
      );
      await screen.findByText('All systems healthy');
      expect(screen.getByTestId('viewport-debt-panel')).toBeInTheDocument();
    });
  });
});
