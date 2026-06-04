import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WarmVmPanel } from './WarmVmPanel';
import { expectNoOverflowAt } from '../test/viewport';
import type { WarmVmStateResponse } from '../types/warmVm';
import type { Project } from '../types/project';

const emptyState: WarmVmStateResponse = {
  active_vms: [],
  warm_pin_metrics: {
    clears_last_hour: 0,
    clears_last_24h: 0,
    active_pinned_queued_total: 0,
    active_warm_chains: 0,
    active_backlog_reuse_claims: 0,
    active_chain_continuity_claims: 0,
  },
  warm_continuity_enabled: true,
  warm_backlog_reuse_enabled: false,
  next_role_poll_timeout_secs: 30,
};

const populatedState: WarmVmStateResponse = {
  active_vms: [
    {
      vm_instance_id: 'vm-instance-aaaaaaaa-bbbb-cccc-dddddddddddd',
      last_heartbeat: new Date(Date.now() - 30_000).toISOString(),
      is_alive: true,
      current_task: {
        id: 'task-running-1',
        title: 'feat: a long current-task title to stress horizontal layout in the table',
        task_type: 'create',
        status: 'IN_PROGRESS',
        warm_start: true,
        warm_claim_origin: 'backlog_reuse',
        prior_role_vm_id: 'vm-prior-eeeeeeee-1234',
        setup_secs: 12.5,
        started_at: new Date(Date.now() - 60_000).toISOString(),
        pr_number: 4321,
        project_id: 'proj-1',
      },
      warm_pinned_queued: [
        { id: 'q-1', task_type: 'review', warm_target_pr: 4321, warm_handoff_deadline: null },
      ],
      recent_completions: [
        { id: 'r-1', task_type: 'create', completed_at: null, status: 'COMPLETED' },
        { id: 'r-2', task_type: 'review-fix', completed_at: null, status: 'FAILED' },
      ],
    },
  ],
  warm_pin_metrics: {
    clears_last_hour: 8,
    clears_last_24h: 24,
    active_pinned_queued_total: 1,
    active_warm_chains: 1,
    active_backlog_reuse_claims: 1,
    active_chain_continuity_claims: 0,
    // All three breakers OPEN so the suppression callouts render into the 375px
    // overflow gate (they are otherwise absent when nothing is suppressed).
    pool_circuit_breaker_suppressed: true,
    pool_suppressed_until: '2026-06-03T12:30:00',
    pool_claim_failures_last_hour: 7,
    pool_failure_rate_threshold: 5,
    circuit_breaker_suppressed: true,
    suppressed_until: '2026-06-03T09:15:00',
    clear_rate_threshold: 10,
    backlog_reuse_circuit_breaker_suppressed: true,
    backlog_reuse_suppressed_until: '2026-06-03T10:45:00',
    backlog_reuse_failures_last_hour: 8,
    backlog_reuse_failure_rate_threshold: 5,
  },
  warm_continuity_enabled: true,
  warm_backlog_reuse_enabled: true,
  next_role_poll_timeout_secs: 30,
  // Per-account warm-pool mix with a long account label to stress the 375px
  // gate: the label must wrap (break-all) rather than force horizontal overflow.
  pool_metrics: {
    enabled: true,
    target_size: 3,
    current_size: 2,
    idle_count: 1,
    projected_idle_cost_usd: 0.18,
    funnel: {
      window_hours: 24,
      pool_lease_hits: 6,
      cold_dispatches: 2,
      total_dispatches: 8,
      pool_hit_rate: 0.75,
      lease_miss_by_reason: {},
      lease_miss_total: 0,
    },
    cost_amortization_by_account: {
      'anthropic-prod-max-plan-account-0001-very-long-label': {
        pool_idle_cost_usd: 0.12,
        pool_lease_hits: 5,
        cold_dispatches: 1,
        total_dispatches: 6,
        pool_hit_rate: 0.8333,
        cost_per_pool_hit_usd: 0.024,
      },
      'anthropic-staging-overflow-account-0002': {
        pool_idle_cost_usd: 0.06,
        pool_lease_hits: 1,
        cold_dispatches: 1,
        total_dispatches: 2,
        pool_hit_rate: 0.5,
        cost_per_pool_hit_usd: 0.06,
      },
    },
  },
} as unknown as WarmVmStateResponse;

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response(JSON.stringify(emptyState), { status: 200 })),
  enableWarmPoolForProject: vi.fn(async () => ({})),
  disableWarmPoolForProject: vi.fn(async () => ({})),
  fetchWarmPoolTargets: vi.fn(async () => ({
    warm_pool_target_size: 2,
    warm_pool_target_size_by_project: {},
  })),
  setWarmPoolTargetForProject: vi.fn(async () => ({})),
  // Returns a payoff verdict so the per-project warm-pool payoff row renders
  // into the 375px overflow gate (it is otherwise absent without a scoped pool).
  fetchEfficiencyAxes: vi.fn(async () => ({
    axes: {},
    pool_payoff_verdict: {
      state: 'COST_REGRESSION',
      supporting: {
        pool_origin_share: 0.45,
        pool_warm_starts: 9,
        pool_hit_rate: 0.42,
        total_pool_dispatches: 64,
        cost_drift_pct: 23.7,
        cost_incl_idle_current: 12.4,
        cost_baseline: 10.0,
      },
      recommended_action:
        'Reduce the pool target or disable — idle-inclusive cost per merged ' +
        'PR has regressed above the pre-pool baseline.',
    },
  })),
  API_BASE_URL: 'http://localhost:8000',
}));

// Return a real efficiency snapshot (with the warm-start origin breakdown) so
// the new "warm by origin" row is rendered into the 375px overflow gate rather
// than being skipped (which it would be when the hook returns null/empty).
vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: () => ({
    computed_at: new Date().toISOString(),
    window_24h: {
      setup_p50_secs: 42.5,
      setup_p95_secs: 88.2,
      setup_sample_n: 20,
      warm_reuse_rate: 0.6,
      warm_reuse_sample_n: 10,
      review_iter_median: 1,
      review_iter_p95: 3,
      review_iter_sample_n: 10,
      vs_prior_setup_p50_delta: null,
      vs_prior_warm_reuse_rate_delta: null,
      vs_prior_review_iter_median_delta: null,
      warm_start_origin_breakdown: {
        warm_pool: 3,
        backlog_reuse: 2,
        chain_continuity: 5,
        warm_total: 10,
        pool_origin_share: 0.3,
      },
    },
    window_7d: {
      setup_p50_secs: null,
      setup_p95_secs: null,
      setup_sample_n: 0,
      warm_reuse_rate: null,
      warm_reuse_sample_n: 0,
      review_iter_median: 2,
      review_iter_p95: 4,
      review_iter_sample_n: 5,
      vs_prior_setup_p50_delta: null,
      vs_prior_warm_reuse_rate_delta: null,
      vs_prior_review_iter_median_delta: null,
    },
  }),
  fmtEffSecs: (s: number | null | undefined) => (s == null ? '—' : `${s}s`),
  fmtEffRate: (r: number | null | undefined) => (r == null ? '—' : `${Math.round(r * 100)}%`),
  fmtEffIter: (n: number | null | undefined) => (n == null ? '—' : n.toFixed(1)),
}));

describe('WarmVmPanel — viewport regression', () => {
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

  test('idle / no active VMs renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<WarmVmPanel onViewTask={() => {}} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });

    // Idle state renders no table, so no escape hatch exists. Verify the
    // panel root inside `host` (not document.body) stays in bounds.
    const panelRoot = host?.firstElementChild as HTMLElement | null;
    expect(panelRoot).not.toBeNull();
    const rect = panelRoot!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(388); // 375px + 12px host padding + 1px tolerance
  });

  test('scoped warm-pool payoff verdict row renders without horizontal overflow at 375px', async () => {
    const scopedState = {
      ...populatedState,
      pool_metrics: {
        ...(populatedState as unknown as { pool_metrics: Record<string, unknown> }).pool_metrics,
        project_allowlist: ['proj-overflow-0001-very-long-project-identifier'],
      },
    } as unknown as WarmVmStateResponse;
    const projects: Project[] = [
      {
        id: 'proj-overflow-0001-very-long-project-identifier',
        name: 'Project With A Deliberately Long Name To Stress The 375px Gate',
        status: 'active', github_owner: 'o', github_repo: 'r',
        autonomous_enabled: true, created_at: '2026-01-01',
      },
    ];
    const { authGet } = await import('../api');
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(scopedState), { status: 200 }),
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });

    const panelRoot = host?.firstElementChild as HTMLElement | null;
    expect(panelRoot).not.toBeNull();
    const rect = panelRoot!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(388);
  });

  test('cost-guard + circuit-breaker banners render without horizontal overflow at 375px', async () => {
    const stateWithBanners = {
      ...emptyState,
      warm_pin_metrics: {
        ...emptyState.warm_pin_metrics,
        pool_circuit_breaker_suppressed: true,
        pool_suppressed_until: new Date(Date.now() + 600_000).toISOString(),
        pool_claim_failures_last_hour: 12,
        pool_failure_rate_threshold: 5,
      },
      pool_metrics: {
        enabled: true,
        target_size: 2,
        current_size: 2,
        idle_count: 2,
        cost_guard: {
          engaged: true,
          reason:
            'Pool hit rate 2.0% is below the 10% floor over 24h (50 dispatches) ' +
            'while idle spend is $5.00 — draining idle members and pausing ' +
            'provisioning until the hit rate recovers.',
          hit_rate: 0.02,
          total_dispatches: 50,
          idle_cost_usd: 5.0,
          min_hit_rate: 0.1,
          min_idle_cost_usd: 1.0,
        },
      },
    } as unknown as WarmVmStateResponse;
    const { authGet } = await import('../api');
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(stateWithBanners), { status: 200 }),
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<WarmVmPanel onViewTask={() => {}} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });

    const panelRoot = host?.firstElementChild as HTMLElement | null;
    expect(panelRoot).not.toBeNull();
    const rect = panelRoot!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(388);
  });

  test('populated VMs + over-threshold clear-rate renders without horizontal overflow at 375px', async () => {
    const { authGet } = await import('../api');
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(populatedState), { status: 200 }),
    );
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<WarmVmPanel onViewTask={() => {}} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });

    const panelRoot = host?.firstElementChild as HTMLElement | null;
    expect(panelRoot).not.toBeNull();
    const rect = panelRoot!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(388); // 375px + 12px host padding + 1px tolerance
  });
});
