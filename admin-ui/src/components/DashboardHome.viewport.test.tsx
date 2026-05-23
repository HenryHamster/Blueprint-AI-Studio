import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DashboardHome } from './DashboardHome';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import { apiFetch } from '../api';
import type { Task } from '../types/task';

const now = Date.now();
// 'task-abc' matches the task_id in the mocked /digest recent_completions payload.
const POPULATED_TASKS: Task[] = [
  {
    id: 'task-abc',
    title: 'feat: add mobile-responsive grid layout to DashboardHome operator page',
    description: 'Refactor the DashboardHome grid to use Tailwind responsive breakpoints.',
    status: 'COMPLETED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 3_600_000).toISOString(),
    started_at: new Date(now - 3_500_000).toISOString(),
  },
  {
    id: 'task-fail-1',
    title: 'fix: digest failure-group pill overflows at 375px',
    description: 'Ensure failure class pills wrap rather than overflow at mobile widths.',
    status: 'FAILED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 2_700_000).toISOString(),
    started_at: new Date(now - 2_600_000).toISOString(),
    failure_class: 'CODE_ERROR',
  },
  {
    id: 'task-fail-2',
    title: 'fix: direction progress card clips title text at 375px',
    description: 'Apply text-wrap and min-w-0 to prevent direction title clipping.',
    status: 'FAILED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 1_900_000).toISOString(),
    started_at: new Date(now - 1_800_000).toISOString(),
    failure_class: 'AGENT_TIMEOUT',
  },
  {
    id: 'task-active-1',
    title: 'perf: reduce digest fetch latency by caching direction_progress',
    description: 'Cache the direction_progress field in Redis to reduce p95 digest latency.',
    status: 'IN_PROGRESS',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 1_000_000).toISOString(),
    started_at: new Date(now - 900_000).toISOString(),
  },
  {
    id: 'task-queued-1',
    title: 'create: add pipeline conversion rate trend sparkline to DashboardHome',
    description: 'Render a 30-day sparkline next to the conversion rate metric.',
    status: 'QUEUED',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 300_000).toISOString(),
  },
];

// DashboardHome (and its sub-components) transitively import apiFetch from
// '../api', which pulls in Firebase. Mock the module so the test does not
// require real auth and so we can control the digest payload.
// NOTE: must be a complete mock — TaskCard (transitively imported via
// `isSafeUrl`) destructures both `API_BASE_URL` and `authGet`, so the mock
// has to provide them or the test file fails to load with a SyntaxError.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/digest')) {
      return new Response(
        JSON.stringify({
          failed_count: 3,
          pending_approval_count: 1,
          direction_progress: [],
          recent_completions: [
            {
              task_id: 'task-abc',
              title: 'feat: add mobile-responsive grid layout to DashboardHome operator page',
              completed_at: '2026-05-06T00:00:00Z',
              pr_url: 'https://github.com/example/repo/pull/42',
              task_type: 'create',
              direction_id: null,
              direction_text: null,
              duration_seconds: 3600,
            },
          ],
          failure_groups: [
            { failure_class: 'CODE_ERROR', label: 'Code Error', count: 2, tasks: [] },
            { failure_class: 'AGENT_TIMEOUT', label: 'Timeout', count: 1, tasks: [] },
          ],
          success_rate: 0.82,
          trend: 'stable',
          trend_history: [],
          completed_count: 24,
          pipeline_conversion_rate: 0.61,
          pipeline_acceptance_rate: 0.74,
          total_estimated_cost_usd: 8.42,
          failure_taxonomy_distribution: {
            categories: {
              code_defect: { count: 2, percentage: 67 },
              scope_related: { count: 1, percentage: 33 },
            },
            total_failures: 3,
            dominant_category: 'code_defect',
          },
        }),
        { status: 200 },
      );
    }
    if (url.includes('/task-duration-stats')) {
      // Shape required by TaskDurationWidget — empty maps yield an early
      // null-render rather than a TypeError on undefined keys.
      return new Response(
        JSON.stringify({
          stats: {},
          scope_duration_stats: {},
          phase_duration_stats: {},
        }),
        { status: 200 },
      );
    }
    if (url.includes('/version-lifecycle')) {
      return new Response(
        JSON.stringify({
          current_version: {
            version_number: 42,
            phase: 'stabilize',
            phase_changed_at: '2026-05-20T12:00:00Z',
            opened_at: '2026-05-15T00:00:00Z',
            qualification_attempts: 2,
            merged_task_count: 15,
            last_qualification: {
              status: 'dry_run',
              confidence: 0.78,
              threshold: 0.80,
              sub_scores: { audit: 0.85, test_erosion: 0.70, smoketest: 0.90 },
              blocker: null,
              finalized_at: '2026-05-20T11:00:00Z',
            },
          },
          recent_versions: [
            { version_number: 41, status: 'promoted', opened_at: '2026-05-10T00:00:00Z', promoted_at: '2026-05-15T00:00:00Z', qualification_attempts: 1, last_qualification: { status: 'promoted', confidence: 0.85, threshold: 0.80, sub_scores: null, blocker: null, finalized_at: null } },
          ],
        }),
        { status: 200 },
      );
    }
    // All other endpoints return 404 so sub-components see their `if (!res.ok)`
    // / `if (!data)` early-exits and render their idle/empty states. Returning
    // `{}` with 200 instead causes widgets that destructure specific shapes
    // (rows, stats, etc.) to throw TypeErrors during render.
    return new Response('not found', { status: 404 });
  }),
  API_BASE_URL: 'http://localhost',
  COST_PER_VM_HR: 0.067,
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  authHeaders: vi.fn(async () => ({})),
  fetchTask: vi.fn(async () => ({})),
  fetchTaskSerialConsole: vi.fn(async () => ({ available: false })),
  fetchAutonomyDebug: vi.fn(async () => ({})),
  fetchRetirementCritiques: vi.fn(async () => ({ critiques: [] })),
  fetchResearchActionability: vi.fn(async () => ({
    cycles: [],
    summary: null,
  })),
  fetchSrMissedRetrospectives: vi.fn(async () => ({ items: [], total: 0 })),
  fetchSrRecentMisses: vi.fn(async () => ({ items: [] })),
  fetchSrUnhandledEdgesRetrospectives: vi.fn(async () => ({ items: [], total: 0 })),
  fetchDirectionSrBreakdown: vi.fn(async () => ({ directions: [] })),
  fetchCrossLayerCompliance: vi.fn(async () => ({
    total_classified: 0,
    compliant: 0,
    non_compliant: 0,
    awaiting_backfill_count: 0,
    compliance_rate: null,
    failure_modes: { doc_test_only: 0, most_recent_layer_only: 0 },
    by_class: {},
  })),
  onboardProject: vi.fn(async () => ({})),
  setVersionPhase: vi.fn(async () => ({
    version_number: 42,
    phase: 'stabilize',
    phase_changed_at: '2026-05-20T12:00:00Z',
    phase_reasoning: '[operator:test] operator advance',
  })),
  triggerQualify: vi.fn(async () => ({ status: 'dry_run' })),
  fetchQualificationVerdicts: vi.fn(async () => ({
    verdicts: [
      { id: 'v1', release_version_id: 'rv1', candidate_sha: 'abc1234def56', confidence: 0.83, sub_scores: { audit: 0.90, test_erosion: 0.75, smoketest: 0.88 }, blocker: null, would_promote: true, promotion_enabled: false, terminal_status: 'dry_run', threshold: 0.80, started_at: '2026-05-20T10:00:00Z', finalized_at: '2026-05-20T11:00:00Z', created_at: '2026-05-20T11:00:00Z' },
      { id: 'v2', release_version_id: 'rv1', candidate_sha: 'def5678abc12', confidence: 0.61, sub_scores: { audit: 0.70, test_erosion: 0.50, smoketest: 0.65 }, blocker: 'test_erosion below threshold', would_promote: false, promotion_enabled: true, terminal_status: 'not_promoted', threshold: 0.80, started_at: '2026-05-19T08:00:00Z', finalized_at: '2026-05-19T09:00:00Z', created_at: '2026-05-19T09:00:00Z' },
    ],
    total: 2,
  })),
  CANONICAL_LAYER_NAMES: ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS', 'VIEWPORT'] as const,
  fetchCanonicalRegressionLayers: vi.fn(async () => ({ layers: [] })),
}));

describe('DashboardHome — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      // Match the app-shell padding applied at mobile (p-3 = 12px each side);
      // narrower than the viewport so we approximate real operator conditions.
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      // Allow async state updates (mock fetches resolve) to settle.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('version lifecycle panel renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId="proj-abc"
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('verdict history panel renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId="proj-abc"
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('operator-facing text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { selector: 'button, a[href]' });
  });

  test('phase-control buttons in version lifecycle panel meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId="proj-abc"
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { selector: '[data-testid="phase-controls"] button' });
  });

  test('direction portfolio cards with realistic data do not overflow at 375px', async () => {
    vi.mocked(apiFetch).mockImplementationOnce(async (url: string) => {
      if (url.includes('/digest')) {
        return new Response(JSON.stringify({
          failed_count: 3,
          pending_approval_count: 2,
          direction_progress: [
            {
              direction_id: 'd-001',
              direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels including kanban board, task detail drawer, and metrics dashboard',
              priority: 'high',
              cycles_in_window: 6,
              tasks_created: 14,
              prs_merged: 8,
              possibly_saturated: false,
              estimated_cost_usd: 42.50,
              conversion_rate: 0.57,
              first_attempt_success_rate: 0.43,
            },
            {
              direction_id: 'd-002',
              direction_text: 'Enforce all viewport-coverage invariants in the test:viewport gate and add escape-hatch debt ceiling guardrail',
              priority: 'medium',
              cycles_in_window: 3,
              tasks_created: 7,
              prs_merged: 3,
              possibly_saturated: true,
              auto_paused: true,
              paused_reason: 'Saturation detected: merge rate below viable threshold for 3 consecutive cycles',
              estimated_cost_usd: 18.20,
              conversion_rate: 0.43,
            },
          ],
          recent_completions: [],
          failure_groups: [{ failure_class: 'CODE_ERROR', label: 'Code Error', count: 3, tasks: [] }],
          success_rate: 0.75,
          trend: 'stable',
          trend_history: [],
          completed_count: 10,
        }), { status: 200 });
      }
      return new Response('not found', { status: 404 });
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DashboardHome
            tasks={POPULATED_TASKS}
            selectedProjectId={null}
            onViewTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
