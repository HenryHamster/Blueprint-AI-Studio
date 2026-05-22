import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { ProjectDashboard } from '../components/ProjectDashboard';
import { CatchUpPanel } from '../components/CatchUpPanel';
import { DiagnosticsPanel } from '../components/DiagnosticsPanel';
import { DashboardHome } from '../components/DashboardHome';
import { PlaybookPanel } from '../components/PlaybookPanel';
import { expectNoOverflowAt } from './viewport';
import type { ProjectDiagnostics } from '../types/diagnostics';
import type { Project } from '../types/project';

// Unified api mock satisfying all four panels in one declaration (a test file
// may only have one vi.mock per module path).
//
// vi.importActual keeps real helpers intact so transitively-imported code
// (e.g. authGet used by TaskCard, which CatchUpPanel imports) resolves
// correctly without blowing up at ESM load time.
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');

  // Both OrchestratorHealthBar (/api/admin/digest?hours=1) and CatchUpPanel
  // (/api/admin/digest?hours=24&project_id=...) hit the same endpoint.
  // Include daily_cost_usd/daily_cost_ceiling_usd so OrchestratorHealthBar's
  // cost-display code-path is exercised, plus the full digest shape for
  // CatchUpPanel. Extra fields are ignored by each consumer.
  const digestPayload = {
    daily_cost_usd: 94.32,
    daily_cost_ceiling_usd: 100,
    window_hours: 24,
    generated_at: '2026-05-06T00:00:00Z',
    completed_count: 5,
    failed_count: 1,
    pending_approval_count: 0,
    research_cycles: 1,
    prs_opened: 3,
    active_count: 0,
    running_count: 0,
    needs_attention: [
      {
        task_id: 'task-fail-1',
        title: 'fix: resolve viewport overflow issue on dashboard',
        reason: 'failed',
        error_snippet: 'Layout constraint violation',
        pr_url: null,
        failure_class: 'CODE_ERROR',
      },
    ],
    recent_failures: [
      {
        task_id: 'task-fail-2',
        title: 'feat: add analytics tracking module',
        failed_at: '2026-05-05T12:30:00Z',
        error_snippet: 'Module import failed',
        failure_class: 'IMPORT_ERROR',
      },
    ],
    failure_groups: [
      { failure_class: 'CODE_ERROR', label: 'Code Error', count: 1, tasks: [] },
    ],
    recent_completions: [
      {
        task_id: 'task-complete-1',
        title: 'feat: mobile-responsive layout for core pages',
        completed_at: '2026-05-06T00:00:00Z',
        pr_url: 'https://github.com/example/repo/pull/1',
        task_type: 'create',
        direction_id: null,
        direction_text: null,
        duration_seconds: 3600,
      },
    ],
    direction_progress: [
      {
        direction_id: 'd-001',
        direction_text: 'Improve admin UI mobile responsiveness across core operator pages',
        priority: 'high',
        cycles_in_window: 4,
        tasks_created: 12,
        prs_merged: 7,
        possibly_saturated: false,
      },
    ],
    active_tasks: [],
    fresh_reviewer_effectiveness: null,
  };

  return {
    ...actual,
    API_BASE_URL: 'http://localhost:8000',
    apiFetch: vi.fn(async (url: string) => {
      if (url.includes('/api/admin/digest')) {
        return new Response(JSON.stringify(digestPayload), { status: 200 });
      }
      // Return 404 for all other URLs so widgets that destructure specific
      // shapes (DashboardHome, PlaybookPanel sub-sections) hit their
      // `if (res.ok)` / `if (!data)` early-exits instead of crashing on
      // undefined properties of an empty `{}` body.
      return new Response('not found', { status: 404 });
    }),
    // DiagnosticsPanel polls /api/admin/failing-task-types via authGet on mount.
    authGet: vi.fn(async () =>
      new Response(
        JSON.stringify({
          gated_types: [],
          global_failure_threshold: 0.5,
          anomaly_tightened: false,
          window_minutes: 60,
          generated_at: '2026-05-06T00:00:00Z',
        }),
        { status: 200 },
      ),
    ),
  };
});

// Realistic props reused from OrchestratorHealthBar.viewport.test.tsx (worst-case state).
const healthBarProps = {
  health: {
    last_cycle_seconds_ago: 132,
    queued_tasks: 18,
    active_tasks: 7,
    pool_info: {},
    deploy_state: {
      status: 'DEGRADED',
      rollback_count: 2,
      rollback_reason: 'rolling back after a long deployment name exposed a layout edge case',
      pause_reason: 'waiting on a very long maintenance window reason for mobile fit testing',
      monitoring_since: '2026-05-06T23:00:00Z',
      recent_outcomes: [],
    },
    deploy_monitoring: {
      external_health: {
        github_api: 'degraded',
        db: 'ok',
        gcp: 'slow',
      },
    },
  },
  detailedHealth: {
    circuit_breaker: {
      projects: {
        'proj-super-long-name-alpha-beta-gamma-delta-epsilon': { state: 'OPEN', failure_count: 9, opened_at: null },
        'proj-very-long-name-zeta-eta-theta-iota-kappa-lambda': { state: 'OPEN', failure_count: 4, opened_at: null },
      },
    },
    max_plan_pool: { total: 6, available: 0, by_provider: { anthropic: { total: 3, available: 0 } } },
    autonomous: { enabled: true, consecutive_failures: 3 },
    provider_apis: {
      status: 'degraded',
      fallback_enabled: true,
      required_providers: ['anthropic', 'openai', 'gemini'],
      available_providers: ['anthropic'],
      degraded_providers: ['openai'],
      down_providers: ['gemini'],
      providers: {
        anthropic: { provider: 'anthropic', dep_name: 'anthropic', configured: true, dispatch_relevant: true, status: 'ok', reason: null, has_raw_credentials: true, total_accounts: 3, enabled_accounts: 3, ready_accounts: 3, available_accounts: 3, primary_model: 'claude-opus-4-1', dispatch_available: true },
        openai: { provider: 'openai', dep_name: 'openai', configured: true, dispatch_relevant: true, status: 'degraded', reason: 'latency is high enough to keep this pill wide', has_raw_credentials: true, total_accounts: 2, enabled_accounts: 2, ready_accounts: 1, available_accounts: 1, primary_model: 'gpt-5.1', dispatch_available: true },
        gemini: { provider: 'gemini', dep_name: 'gcp', configured: true, dispatch_relevant: true, status: 'down', reason: 'service account credentials missing', has_raw_credentials: false, total_accounts: 1, enabled_accounts: 1, ready_accounts: 0, available_accounts: 0, primary_model: 'gemini-2.5-pro', dispatch_available: false },
      },
    },
    github_quota: {
      remaining: 17,
      limit: 5000,
      reset_at: 1778131200,
      last_updated: '2026-05-06T23:10:00Z',
      status: 'critical',
    },
  },
  apiHealthy: true,
  wsConnected: false,
  lastFetchTime: Date.UTC(2026, 4, 7, 0, 0, 0),
  tasks: [
    { id: 'task-1', status: 'IN_PROGRESS', last_heartbeat: '2026-05-06T23:40:00Z' },
    { id: 'task-2', status: 'IN_PROGRESS', last_heartbeat: '2026-05-06T22:30:00Z' },
    { id: 'task-3', status: 'QUEUED', last_heartbeat: null },
  ],
  pipelineThrottle: {
    reason: 'deploy',
    detail: 'pipeline blocked by a long-running deployment gate reason that should still wrap cleanly',
    recovery_seconds: 98,
  },
};

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [],
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

// Realistic diagnostics reused from DiagnosticsPanel.viewport.test.tsx.
const diagnosticsData: ProjectDiagnostics = {
  project_id: 'proj-1',
  stuck_tasks: [
    {
      task_id: 'task-very-long-id-that-might-overflow-on-mobile-screens-abc-123',
      title: 'feat: a sufficiently long stuck-task title that exercises the truncate utility on narrow screens',
      status: 'IN_PROGRESS',
      stuck_reason: 'no_heartbeat',
      stuck_for_seconds: 4500,
      attempt_count: 3,
      failure_class: null,
      unresolvable_deps: [],
      remediation_hint: 'check VM heartbeat — this hint is intentionally long to make sure it wraps cleanly on mobile',
    },
    {
      task_id: 'task-2',
      title: 'fix: another stuck task',
      status: 'IN_PROGRESS',
      stuck_reason: 'stuck_deps',
      stuck_for_seconds: 200,
      attempt_count: 1,
      failure_class: null,
      unresolvable_deps: ['dep-aaaaaaaa-1111', 'dep-bbbbbbbb-2222'],
    },
  ],
  circuit_breaker: { state: 'OPEN', failure_count: 7, opened_at: null },
  queued_count: 4,
  in_progress_count: 2,
  zone_health: { available: 1, exhausted: ['us-central1-a', 'us-central1-b'] },
};

describe('operator home — composed viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('OrchestratorHealthBar + ProjectDashboard + CatchUpPanel + DiagnosticsPanel compose without horizontal overflow at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'paused', autonomous_enabled: false }),
      makeProject({ id: 'p3', name: 'Data Pipeline', status: 'active' }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      // Replicate App.tsx's mobile shell className. The shell's p-3 (12px)
      // provides the padding — do NOT additionally set host.style.padding the
      // way isolated per-component tests do (those add it manually because
      // they have no shell; here the shell provides it).
      host.className = 'flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <>
            {/* HeaderBar stub — has its own thorough viewport test; 30+ props would balloon this file */}
            <div data-testid="header-bar-stub" />
            <OrchestratorHealthBar {...healthBarProps} />
            <ProjectDashboard
              projects={projects}
              tasks={[]}
              selectedProjectId="p1"
              onSelectProject={() => {}}
            />
            <CatchUpPanel
              selectedProjectId="p1"
              projects={projects}
              lastVisitTimestamp={Date.now() - 24 * 60 * 60 * 1000}
              refreshInterval={60000}
              onSelectTask={() => {}}
              needsAttentionCount={1}
              onApprove={() => {}}
              onRetry={() => {}}
              onBulkApprove={() => {}}
              onBulkRetry={() => {}}
            />
            <DiagnosticsPanel diagnostics={diagnosticsData} onViewTask={() => {}} />
            {/* Home body panels — rendered beneath the chrome to cover the highest-traffic landing page */}
            <DashboardHome
              tasks={[]}
              selectedProjectId="p1"
              projects={projects}
              onViewTask={() => {}}
            />
            <PlaybookPanel selectedProjectId="p1" />
          </>,
        );
      });
      // Two act-wrapped settles: the first lets the panels' mount-time
      // fetches resolve; the second absorbs the follow-up state update
      // PipelineRegressionsPanel (inside CatchUpPanel) schedules once its
      // initial fetch lands, so no async update escapes act().
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });

    // Positive content: confirm panels rendered real content (not ErrorBoundary fallbacks).
    // A silent crash would fail both the error-boundary sentinel above and these checks.
    expect(host!.textContent).toContain('fix: resolve viewport overflow issue on dashboard');
    expect(host!.textContent).toContain('feat: a sufficiently long stuck-task title');
    // Confirm DashboardHome body rendered: direction from digestPayload should appear.
    expect(host!.textContent).toContain('Improve admin UI mobile responsiveness');

    localStorage.removeItem('projectDashboardCollapsed');
  });
});
