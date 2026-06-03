import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { WarmVmPanel } from './WarmVmPanel';
import type { WarmVmStateResponse, BacklogReuseFunnelSnapshot } from '../types/warmVm';

const baseState: WarmVmStateResponse = {
  active_vms: [],
  warm_pin_metrics: {
    clears_last_hour: 0,
    clears_last_24h: 0,
    active_pinned_queued_total: 0,
    active_warm_chains: 3,
    active_backlog_reuse_claims: 2,
    active_chain_continuity_claims: 1,
  },
  warm_continuity_enabled: true,
  warm_backlog_reuse_enabled: false,
  next_role_poll_timeout_secs: 30,
};

const stateWithFunnel: WarmVmStateResponse = {
  ...baseState,
  chain_continuity_funnel: {
    window_hours: 24,
    poll_arrivals: 42,
    chain_claimed: 7,
    no_pinned_task: 15,
    another_vm_owns_pr: 3,
    doomed_vm: 1,
    skipped_by_reason: { disabled: 5, breaker_tripped: 11 },
    skipped_total: 16,
  },
};

const stateWithPoolMisses: WarmVmStateResponse = {
  ...baseState,
  pool_metrics: {
    enabled: true,
    target_size: 2,
    current_size: 2,
    idle_count: 2,
    funnel: {
      window_hours: 24,
      pool_lease_hits: 5,
      cold_dispatches: 6,
      total_dispatches: 11,
      pool_hit_rate: 0.4545,
      lease_miss_by_reason: { no_idle_member: 6 },
      lease_miss_total: 6,
    },
  },
};

// Mutable cell so the payoff-verdict describe can inject an efficiency-axes
// response (optionally carrying pool_payoff_verdict) without affecting the rest
// of the suite, which keeps the default (no verdict → row absent).
const axesCell = vi.hoisted(() => ({ value: { axes: {} } as unknown }));
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response(JSON.stringify(baseState), { status: 200 })),
  enableWarmPoolForProject: vi.fn(async () => ({})),
  disableWarmPoolForProject: vi.fn(async () => ({})),
  fetchWarmPoolTargets: vi.fn(async () => ({
    warm_pool_target_size: 2,
    warm_pool_target_size_by_project: {},
  })),
  setWarmPoolTargetForProject: vi.fn(async () => ({})),
  fetchEfficiencyAxes: vi.fn(async () => axesCell.value),
  API_BASE_URL: 'http://localhost:8000',
}));

// useEfficiencySnapshot hits the network on mount; stub it out so the panel
// renders deterministically. The returned value is held in a mutable cell so a
// dedicated describe can inject a snapshot carrying warm_start_origin_breakdown
// while the rest of the suite keeps the default (no efficiency row).
const effCell = vi.hoisted(() => ({ value: null as unknown }));
vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: () => effCell.value,
  fmtEffSecs: () => '—',
  fmtEffRate: () => '—',
  fmtEffIter: () => '—',
}));

import { apiFetch, authGet, enableWarmPoolForProject, disableWarmPoolForProject, fetchWarmPoolTargets, setWarmPoolTargetForProject, fetchEfficiencyAxes } from '../api';
import type { Project } from '../types/project';

describe('WarmVmPanel — backlog-reuse toggle + attribution', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('backlog-reuse toggle PATCHes warm_backlog_reuse_enabled independently', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    // Wait for the data-gated metrics row so the toggle button is enabled
    // (it is disabled until `data` loads).
    await screen.findByText('backlog reuse');
    const btn = screen.getByText('NO-REUSE');
    fireEvent.click(btn);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        'http://localhost:8000/api/admin/settings',
        expect.objectContaining({ method: 'PATCH' }),
      );
    });
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    // Flips ONLY the backlog flag — must not touch warm_continuity_enabled.
    expect(body).toEqual({ warm_backlog_reuse_enabled: true });
  });

  test('continuity toggle PATCHes warm_continuity_enabled, not the backlog flag', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    const btn = screen.getByText('WARM');
    fireEvent.click(btn);

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    expect(body).toEqual({ warm_continuity_enabled: false });
  });

  test('renders backlog-reuse and chain-claim attribution counts', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    const backlogLabel = screen.getByText('backlog reuse');
    expect(backlogLabel.parentElement?.textContent).toContain('2');
    const chainLabel = screen.getByText('chain claims');
    expect(chainLabel.parentElement?.textContent).toContain('1');
  });

  test('renders pool-origin claim count and share as their own bucket', async () => {
    const stateWithPoolOrigin: WarmVmStateResponse = {
      ...baseState,
      warm_pin_metrics: {
        ...baseState.warm_pin_metrics,
        active_warm_chains: 4,
        active_backlog_reuse_claims: 1,
        active_pool_origin_claims: 2,
        active_chain_continuity_claims: 1,
        pool_origin_share: 0.5,
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPoolOrigin), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const poolChip = await screen.findByTestId('pool-origin-claims');
    // Pool-origin count and its share render in the dedicated bucket.
    expect(poolChip.textContent).toContain('pool origin');
    expect(poolChip.textContent).toContain('2');
    expect(poolChip.textContent).toContain('50%');
    // Chain claims still render their own (now pool-excluded) count.
    expect(screen.getByText('chain claims').parentElement?.textContent).toContain('1');
  });

  test('pool-origin current_task renders a pool label, not chain', async () => {
    const stateWithPoolTask: WarmVmStateResponse = {
      ...baseState,
      active_vms: [
        {
          vm_instance_id: 'agent-vm-pool-1',
          last_heartbeat: new Date().toISOString(),
          is_alive: true,
          current_task: {
            id: 'task-pool-1',
            title: 'feat: a pool-served task',
            task_type: 'create',
            status: 'IN_PROGRESS',
            warm_start: true,
            warm_claim_origin: 'warm_pool',
            prior_role_vm_id: 'agent-vm-pool-1',
            setup_secs: 1.4,
            started_at: new Date().toISOString(),
            pr_number: null,
            project_id: 'proj-1',
          },
          warm_pinned_queued: [],
          recent_completions: [],
        },
      ],
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPoolTask), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    // The pool-origin task surfaces a "pool" label and NOT a "chain" label.
    expect(screen.getAllByText('pool').length).toBeGreaterThan(0);
    expect(screen.queryByText('chain')).toBeNull();
  });

  test('renders chain continuity funnel when present in response', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithFunnel), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('Chain Continuity Funnel (24h)');
    expect(screen.getByText('polls').parentElement?.textContent).toContain('42');
    expect(screen.getByText('claimed').parentElement?.textContent).toContain('7');
    expect(screen.getByText('no pinned').parentElement?.textContent).toContain('15');
    expect(screen.getByText('other VM').parentElement?.textContent).toContain('3');
    expect(screen.getByText('doomed').parentElement?.textContent).toContain('1');
    expect(screen.getByText('skip disabled').parentElement?.textContent).toContain('5');
    expect(screen.getByText('skip breaker').parentElement?.textContent).toContain('11');
  });

  test('renders without crash when chain_continuity_funnel is absent', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByText('Chain Continuity Funnel (24h)')).toBeNull();
  });

  test('renders pool lease-miss reasons when the funnel reports misses', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPoolMisses), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const misses = await screen.findByTestId('pool-lease-misses');
    expect(misses.textContent).toContain('misses');
    expect(misses.textContent).toContain('6');
    expect(misses.textContent).toContain('no_idle_member');
  });

  test('renders spawn pin funnel with mixed skip reasons when present', async () => {
    const stateWithSpawnFunnel: WarmVmStateResponse = {
      ...baseState,
      spawn_pin_funnel: {
        window_hours: 24,
        spawn_attempts: 50,
        pin_set: 30,
        skipped_by_reason: {
          disabled: 4,
          breaker_tripped: 6,
          source_cold: 7,
          stale_source: 3,
          caller_suppressed: 0,
        },
        skipped_total: 20,
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithSpawnFunnel), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('Spawn Pin Funnel (24h)');
    expect(screen.getByText('attempts').parentElement?.textContent).toContain('50');
    expect(screen.getByText('pin set').parentElement?.textContent).toContain('30');
    expect(screen.getByText('skip disabled').parentElement?.textContent).toContain('4');
    expect(screen.getByText('skip breaker').parentElement?.textContent).toContain('6');
    expect(screen.getByText('skip source cold').parentElement?.textContent).toContain('7');
    expect(screen.getByText('skip stale source').parentElement?.textContent).toContain('3');
    // Zero-count skip reason MUST NOT render.
    expect(screen.queryByText('skip suppressed')).toBeNull();
  });

  test('renders without crash when spawn_pin_funnel is absent', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByText(/Spawn Pin Funnel/)).toBeNull();
  });

  test('renders backlog reuse funnel with stage counters and dynamic skip reasons', async () => {
    const stateWithBacklogFunnel: WarmVmStateResponse = {
      ...baseState,
      backlog_reuse_funnel: {
        window_hours: 24,
        completed_vms_with_eligible_backlog: 80,
        teardown_defers_scheduled: 60,
        next_role_polls_during_defer: 45,
        warm_claims_succeeded: 33,
        claim_skipped_by_reason: {
          disabled: 4,
          breaker_tripped: 2,
          source_cold: 5,
          zero_reason: 0,
        },
        claim_skipped_total: 11,
        scan_polls_with_depends_on_skip: 7,
        scan_polls_with_pinned_vm_skip: 3,
        scan_polls_with_file_conflict_skip: 0,
        scan_polls_saturated: 2,
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithBacklogFunnel), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('Backlog Reuse Funnel (24h)');
    expect(screen.getByText('eligible').parentElement?.textContent).toContain('80');
    expect(screen.getByText('defers').parentElement?.textContent).toContain('60');
    expect(screen.getByText('polls').parentElement?.textContent).toContain('45');
    expect(screen.getByText('claimed').parentElement?.textContent).toContain('33');
    // Skip reasons render dynamically — including ones not hardcoded in the component.
    expect(screen.getByTestId('backlog-reuse-skip-disabled').textContent).toContain('4');
    expect(screen.getByTestId('backlog-reuse-skip-breaker_tripped').textContent).toContain('2');
    expect(screen.getByTestId('backlog-reuse-skip-source_cold').textContent).toContain('5');
    // Zero-count reason MUST NOT render.
    expect(screen.queryByTestId('backlog-reuse-skip-zero_reason')).toBeNull();
    // Eligibility-scan skip chips: non-zero render with their counts.
    expect(screen.getByTestId('backlog-reuse-scan-depends_on').textContent).toContain('7');
    expect(screen.getByTestId('backlog-reuse-scan-pinned_vm').textContent).toContain('3');
    expect(screen.getByTestId('backlog-reuse-scan-saturated').textContent).toContain('2');
    // Zero-count scan stage MUST NOT render.
    expect(screen.queryByTestId('backlog-reuse-scan-file_conflict')).toBeNull();
  });

  test('renders without crash when backlog_reuse_funnel is absent', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByText(/Backlog Reuse Funnel/)).toBeNull();
  });

  test('does not render backlog reuse funnel when backend returned empty object', async () => {
    // The backend defensive-import helper returns {} on failure; guard with
    // window_hours so the row stays hidden in that case.
    const stateWithEmptyFunnel = {
      ...baseState,
      backlog_reuse_funnel: {} as BacklogReuseFunnelSnapshot,
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithEmptyFunnel), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByText(/Backlog Reuse Funnel/)).toBeNull();
  });

  test('renders per-reason clear chips for non-zero reasons only', async () => {
    const stateWithReasons: WarmVmStateResponse = {
      ...baseState,
      warm_pin_metrics: {
        ...baseState.warm_pin_metrics,
        clears_last_hour: 3,
        clears_by_reason_last_hour: {
          deadline_expired: 2,
          vm_unresponsive: 1,
          circuit_breaker: 0,
        },
        clears_by_reason_last_24h: {
          deadline_expired: 2,
          vm_unresponsive: 1,
        },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithReasons), { status: 200 }),
    );

    render(<WarmVmPanel onViewTask={() => {}} />);

    // Non-zero reason chips render with the reason label and count.
    const deadlineChip = await screen.findByTestId(
      'clears-by-reason-deadline_expired',
    );
    expect(deadlineChip.textContent).toContain('deadline_expired');
    expect(deadlineChip.textContent).toContain('2');
    const unresponsiveChip = screen.getByTestId(
      'clears-by-reason-vm_unresponsive',
    );
    expect(unresponsiveChip.textContent).toContain('1');

    // Zero-count reason MUST NOT render.
    expect(screen.queryByTestId('clears-by-reason-circuit_breaker')).toBeNull();

    // Existing total chip still renders.
    expect(screen.getByText('clears/hr').parentElement?.textContent).toContain(
      '3',
    );
  });

  test('renders no per-reason chips when histogram is empty', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(
      screen.queryByTestId('clears-by-reason-deadline_expired'),
    ).toBeNull();
    expect(
      screen.queryByTestId('clears-by-reason-vm_unresponsive'),
    ).toBeNull();
  });
});

describe('WarmVmPanel — warm-pool toggle + pool metrics', () => {
  const stateWithPool: WarmVmStateResponse = {
    ...baseState,
    pool_metrics: {
      enabled: true,
      target_size: 5,
      current_size: 4,
      idle_count: 3,
      idle_ttl_secs: 1800,
      projected_idle_cost_usd: 0.1234,
      funnel: {
        window_hours: 24,
        pool_lease_hits: 12,
        cold_dispatches: 4,
        total_dispatches: 16,
        pool_hit_rate: 0.75,
        lease_miss_by_reason: {},
        lease_miss_total: 0,
      },
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('pool toggle PATCHes warm_pool_enabled independently (enable from NO-POOL)', async () => {
    // baseState carries no pool_metrics → poolOn is false → button reads NO-POOL.
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    const btn = screen.getByText('NO-POOL');
    fireEvent.click(btn);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        'http://localhost:8000/api/admin/settings',
        expect.objectContaining({ method: 'PATCH' }),
      );
    });
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    // Flips ONLY the pool flag — must not touch sibling warm flags (the
    // independent-flip regression class the backlog-reuse test also guards).
    expect(body).toEqual({ warm_pool_enabled: true });
  });

  test('pool toggle DISABLEs warm_pool_enabled when currently enabled', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPool), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const btn = await screen.findByText('POOL');
    fireEvent.click(btn);

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    expect(body).toEqual({ warm_pool_enabled: false });
  });

  test('renders PoolMetricsRow with size, idle, cost, and hit rate', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPool), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const row = await screen.findByTestId('pool-metrics-row');
    expect(row.textContent).toContain('Warm Pool (enabled)');
    expect(screen.getByText('size').parentElement?.textContent).toContain('4');
    expect(screen.getByText('size').parentElement?.textContent).toContain('5');
    expect(screen.getByText('idle').parentElement?.textContent).toContain('3');
    // 0.1234 → "$0.12" (>= $0.01 branch, 2dp).
    expect(screen.getByTestId('pool-idle-cost').textContent).toBe('$0.12');
    // hit rate 0.75 → 75%.
    expect(screen.getByText('hit rate').parentElement?.textContent).toContain('75%');
    expect(screen.getByText('hits').parentElement?.textContent).toContain('12');
    expect(screen.getByText('cold').parentElement?.textContent).toContain('4');
  });

  test('idle cost and hit rate fall back to em-dash when absent/null', async () => {
    const stateNoCost: WarmVmStateResponse = {
      ...baseState,
      pool_metrics: {
        enabled: true,
        target_size: 2,
        current_size: 0,
        idle_count: 0,
        // projected_idle_cost_usd omitted → fmtUsd returns '—'.
        funnel: {
          window_hours: 24,
          pool_lease_hits: 0,
          cold_dispatches: 0,
          total_dispatches: 0,
          pool_hit_rate: null, // no dispatches yet → '—'.
          lease_miss_by_reason: {},
          lease_miss_total: 0,
        },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateNoCost), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.getByTestId('pool-idle-cost').textContent).toBe('—');
    expect(screen.getByText('hit rate').parentElement?.textContent).toContain('—');
  });

  test('does not render PoolMetricsRow when pool_metrics is absent', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('pool-metrics-row')).toBeNull();
    // The toggle still renders in its disabled (NO-POOL) state.
    expect(screen.getByText('NO-POOL')).toBeTruthy();
  });

  test('renders per-account warm-pool mix with hit rate, dispatches, and idle cost', async () => {
    const stateWithAccountMix = {
      ...stateWithPool,
      pool_metrics: {
        ...stateWithPool.pool_metrics!,
        // Mirrors backend cost_amortization_by_account (busiest account first
        // is enforced by the component's sort, not the payload order).
        cost_amortization_by_account: {
          'acct-low': {
            pool_idle_cost_usd: 0.04,
            pool_lease_hits: 1,
            cold_dispatches: 1,
            total_dispatches: 2,
            pool_hit_rate: 0.5,
            cost_per_pool_hit_usd: 0.04,
          },
          'acct-busy': {
            pool_idle_cost_usd: 0.2,
            pool_lease_hits: 9,
            cold_dispatches: 1,
            total_dispatches: 10,
            pool_hit_rate: 0.9,
            cost_per_pool_hit_usd: 0.0222,
          },
          'acct-cold': {
            pool_idle_cost_usd: 0.0,
            pool_lease_hits: 0,
            cold_dispatches: 0,
            total_dispatches: 0,
            pool_hit_rate: null, // no dispatch yet → em-dash, $/warm em-dash.
            cost_per_pool_hit_usd: null,
          },
        },
      },
    } as unknown as WarmVmStateResponse;
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithAccountMix), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-account-mix-row');
    // Busiest account sorts first.
    const rows = screen.getAllByTestId(/^pool-account-acct-/);
    expect(rows[0].getAttribute('data-testid')).toBe('pool-account-acct-busy');

    const busy = screen.getByTestId('pool-account-acct-busy');
    expect(busy.textContent).toContain('90%'); // 0.9 hit rate
    expect(busy.textContent).toContain('9/10'); // warm/total dispatches
    expect(busy.textContent).toContain('$0.20'); // idle cost

    // Account with no dispatch yet shows em-dash hit rate and $/warm.
    const cold = screen.getByTestId('pool-account-acct-cold');
    expect(cold.textContent).toContain('—');
  });

  test('does not render per-account mix when cost_amortization_by_account is empty', async () => {
    // stateWithPool carries pool_metrics but no per-account breakdown.
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithPool), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('pool-account-mix-row')).toBeNull();
  });
});

describe('WarmVmPanel — warm-pool retarget toggles + per-account misses', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('account-retarget toggle PATCHes warm_pool_account_retarget_enabled only', async () => {
    // baseState omits the flag → defaults false → button reads NO-RETARGET.
    render(<WarmVmPanel onViewTask={() => {}} />);

    const btn = await screen.findByTestId('warm-pool-account-retarget-toggle');
    expect(btn.textContent).toBe('NO-RETARGET');
    fireEvent.click(btn);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        'http://localhost:8000/api/admin/settings',
        expect.objectContaining({ method: 'PATCH' }),
      );
    });
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    // Flips ONLY the account-retarget flag — must not touch sibling flags.
    expect(body).toEqual({ warm_pool_account_retarget_enabled: true });
  });

  test('cross-provider toggle PATCHes warm_pool_cross_provider_retarget_enabled only', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    const btn = await screen.findByTestId('warm-pool-cross-provider-retarget-toggle');
    expect(btn.textContent).toBe('SAME-PROV');
    fireEvent.click(btn);

    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    expect(body).toEqual({ warm_pool_cross_provider_retarget_enabled: true });
  });

  test('toggles reflect the current enabled state from the payload', async () => {
    const stateRetargetOn: WarmVmStateResponse = {
      ...baseState,
      warm_pool_account_retarget_enabled: true,
      warm_pool_cross_provider_retarget_enabled: true,
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateRetargetOn), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const acct = await screen.findByTestId('warm-pool-account-retarget-toggle');
    expect(acct.textContent).toBe('RETARGET');
    expect(screen.getByTestId('warm-pool-cross-provider-retarget-toggle').textContent).toBe('X-PROV');

    // Flipping an enabled flag DISABLEs it (and only it).
    fireEvent.click(acct);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(opts.body as string)).toEqual({ warm_pool_account_retarget_enabled: false });
  });

  test('renders per-account lease-miss breakdown from the funnel by_account', async () => {
    const stateWithAccountMisses = {
      ...baseState,
      pool_metrics: {
        enabled: true,
        target_size: 2,
        current_size: 2,
        idle_count: 2,
        funnel: {
          window_hours: 24,
          pool_lease_hits: 1,
          cold_dispatches: 9,
          total_dispatches: 10,
          pool_hit_rate: 0.1,
          lease_miss_by_reason: { no_idle_member: 7, cost_guard: 2 },
          lease_miss_total: 9,
          by_account: {
            'acct-busy': {
              pool_lease_hits: 1,
              cold_dispatches: 9,
              total_dispatches: 10,
              pool_hit_rate: 0.1,
              lease_miss_by_reason: { no_idle_member: 7, cost_guard: 2 },
              lease_miss_total: 9,
            },
          },
        },
        cost_amortization_by_account: {
          'acct-busy': {
            pool_idle_cost_usd: 0.2,
            pool_lease_hits: 1,
            cold_dispatches: 9,
            total_dispatches: 10,
            pool_hit_rate: 0.1,
            cost_per_pool_hit_usd: 0.2,
          },
        },
      },
    } as unknown as WarmVmStateResponse;
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithAccountMisses), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const misses = await screen.findByTestId('pool-account-misses-acct-busy');
    expect(misses.textContent).toContain('misses');
    expect(misses.textContent).toContain('9');
    // Dominant reason sorts first (no_idle_member 7 before cost_guard 2).
    expect(misses.textContent).toContain('no_idle_member 7');
    expect(misses.textContent).toContain('cost_guard 2');
    const text = misses.textContent ?? '';
    expect(text.indexOf('no_idle_member')).toBeLessThan(text.indexOf('cost_guard'));
  });
});

describe('WarmVmPanel — cost-guard + warm-breaker callouts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('renders the cost-guard banner with reason and floors when engaged', async () => {
    const stateGuardEngaged: WarmVmStateResponse = {
      ...baseState,
      pool_metrics: {
        enabled: true,
        target_size: 2,
        current_size: 2,
        idle_count: 2,
        funnel: {
          window_hours: 24,
          pool_lease_hits: 1,
          cold_dispatches: 49,
          total_dispatches: 50,
          pool_hit_rate: 0.02,
          lease_miss_by_reason: {},
          lease_miss_total: 0,
        },
        cost_guard: {
          engaged: true,
          reason: 'Pool hit rate 2.0% is below the 10% floor over 24h (50 dispatches) while idle spend is $5.00 — draining idle members.',
          hit_rate: 0.02,
          total_dispatches: 50,
          idle_cost_usd: 5.0,
          min_hit_rate: 0.1,
          min_idle_cost_usd: 1.0,
        },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateGuardEngaged), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const banner = await screen.findByTestId('pool-cost-guard-banner');
    expect(banner.textContent).toContain('cost guard engaged');
    expect(banner.textContent).toContain('below the 10% floor');
    // hit rate 2% vs floor 10%.
    expect(screen.getByTestId('pool-cost-guard-hit-rate').textContent).toContain('2%');
    expect(screen.getByTestId('pool-cost-guard-hit-rate').textContent).toContain('floor 10%');
    // idle spend $5.00 vs floor $1.00.
    expect(screen.getByTestId('pool-cost-guard-idle-cost').textContent).toContain('$5.00');
    expect(screen.getByTestId('pool-cost-guard-idle-cost').textContent).toContain('$1.00');
  });

  test('does not render the cost-guard banner when disengaged or omitted', async () => {
    const stateGuardOff: WarmVmStateResponse = {
      ...baseState,
      pool_metrics: {
        enabled: true,
        target_size: 2,
        current_size: 2,
        idle_count: 2,
        cost_guard: {
          engaged: false,
          reason: null,
          hit_rate: 0.5,
          total_dispatches: 50,
          idle_cost_usd: 5.0,
          min_hit_rate: 0.1,
          min_idle_cost_usd: 1.0,
        },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateGuardOff), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('pool-cost-guard-banner')).toBeNull();
  });

  test('does not render the cost-guard banner when the field is absent (older backend)', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('pool-cost-guard-banner')).toBeNull();
  });

  test('renders the pool breaker callout with reset time and failures/threshold', async () => {
    const state: WarmVmStateResponse = {
      ...baseState,
      warm_pin_metrics: {
        ...baseState.warm_pin_metrics,
        pool_circuit_breaker_suppressed: true,
        pool_suppressed_until: '2026-06-03T12:30:00',
        pool_claim_failures_last_hour: 7,
        pool_failure_rate_threshold: 5,
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(state), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const callout = await screen.findByTestId('pool-breaker-callout');
    expect(callout.textContent).toContain('OPEN');
    expect(callout.textContent).toContain('7/5 failures');
    // Reset time is rendered (local-time formatting of the naive-UTC stamp).
    expect(callout.textContent).toMatch(/until \d/);
    // Only the pool breaker is open here — the others must not render.
    expect(screen.queryByTestId('continuity-breaker-callout')).toBeNull();
    expect(screen.queryByTestId('backlog-breaker-callout')).toBeNull();
  });

  test('renders continuity and backlog breaker callouts when both are suppressed', async () => {
    const state: WarmVmStateResponse = {
      ...baseState,
      warm_pin_metrics: {
        ...baseState.warm_pin_metrics,
        clears_last_hour: 14,
        circuit_breaker_suppressed: true,
        suppressed_until: '2026-06-03T09:15:00',
        clear_rate_threshold: 10,
        backlog_reuse_circuit_breaker_suppressed: true,
        backlog_reuse_suppressed_until: null,
        backlog_reuse_failures_last_hour: 8,
        backlog_reuse_failure_rate_threshold: 5,
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(state), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const continuity = await screen.findByTestId('continuity-breaker-callout');
    expect(continuity.textContent).toContain('14/10 failures');
    const backlog = screen.getByTestId('backlog-breaker-callout');
    expect(backlog.textContent).toContain('8/5 failures');
    // No until-time when the backend reports a null reset timestamp.
    expect(backlog.textContent).not.toMatch(/until/);
    expect(screen.queryByTestId('pool-breaker-callout')).toBeNull();
  });

  test('renders no callouts when no breaker is suppressed (backward-compat fixture)', async () => {
    // baseState omits every breaker field — an older backend / clean state.
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('warm-breaker-callouts')).toBeNull();
    expect(screen.queryByTestId('pool-breaker-callout')).toBeNull();
    expect(screen.queryByTestId('continuity-breaker-callout')).toBeNull();
    expect(screen.queryByTestId('backlog-breaker-callout')).toBeNull();
  });
});

describe('WarmVmPanel — warm-start origin breakdown', () => {
  const snapshotWithBreakdown = {
    computed_at: new Date().toISOString(),
    window_24h: {
      setup_p50_secs: null,
      setup_p95_secs: null,
      setup_sample_n: 0,
      warm_reuse_rate: 0.6,
      warm_reuse_sample_n: 10,
      review_iter_median: null,
      review_iter_p95: null,
      review_iter_sample_n: 0,
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
      review_iter_median: null,
      review_iter_p95: null,
      review_iter_sample_n: 0,
      vs_prior_setup_p50_delta: null,
      vs_prior_warm_reuse_rate_delta: null,
      vs_prior_review_iter_median_delta: null,
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    effCell.value = null;
    cleanup();
    vi.restoreAllMocks();
  });

  test('renders pool/backlog/chain counts and pool share when breakdown is present', async () => {
    effCell.value = snapshotWithBreakdown;
    render(<WarmVmPanel onViewTask={() => {}} />);

    const row = await screen.findByTestId('warm-start-origin-breakdown');
    // pool=emerald 3, backlog=sky 2, chain=orange 5.
    expect(screen.getByText('pool').parentElement?.textContent).toContain('3');
    expect(screen.getByText('backlog').parentElement?.textContent).toContain('2');
    expect(screen.getByText('chain').parentElement?.textContent).toContain('5');
    // pool_origin_share 0.3 → 30%.
    expect(screen.getByTestId('warm-start-pool-share').textContent).toContain('30%');
    expect(row.textContent).toContain('warm by origin');
  });

  test('does not render the breakdown row when the field is absent (older backend)', async () => {
    effCell.value = {
      ...snapshotWithBreakdown,
      window_24h: {
        ...snapshotWithBreakdown.window_24h,
        warm_start_origin_breakdown: undefined,
      },
    };
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
  });

  test('does not render the breakdown row when the field is null (no warm starts)', async () => {
    effCell.value = {
      ...snapshotWithBreakdown,
      window_24h: {
        ...snapshotWithBreakdown.window_24h,
        warm_start_origin_breakdown: null,
      },
    };
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
  });
});

describe('WarmVmPanel — pre-enable pool hit-rate estimate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    effCell.value = null;
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const stateWithProjectVm: WarmVmStateResponse = {
    ...baseState,
    active_vms: [
      {
        vm_instance_id: 'agent-vm-est-1',
        last_heartbeat: new Date().toISOString(),
        is_alive: true,
        current_task: {
          id: 'task-est-1',
          title: 'feat: a task',
          task_type: 'create',
          status: 'IN_PROGRESS',
          warm_start: false,
          warm_claim_origin: null,
          prior_role_vm_id: null,
          setup_secs: null,
          started_at: new Date().toISOString(),
          pr_number: null,
          project_id: 'proj-estimate-1',
        },
        warm_pinned_queued: [],
        recent_completions: [],
      },
    ],
  };

  const estimateResponse = {
    project_id: 'proj-estimate-1',
    target_size: 5,
    window_days: 7,
    replenish_seconds: 120,
    idle_ttl_seconds: 1800,
    total_dispatches: 8,
    estimated_warm_hits: 6,
    estimated_hit_rate: 0.75,
    by_account: {},
    account_retarget_scenario: { estimated_warm_hits: 7, estimated_hit_rate: 0.875 },
    assumptions: 'coarse estimate',
  };

  test('runs the estimate with the chosen target size and renders the hit rate', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url.includes('/pool-hit-rate-estimate')) {
        return new Response(JSON.stringify(estimateResponse), { status: 200 });
      }
      return new Response(JSON.stringify(stateWithProjectVm), { status: 200 });
    });

    render(<WarmVmPanel onViewTask={() => {}} />);

    // The estimate control renders once data (with a project-bearing VM) loads.
    const targetInput = await screen.findByTestId('pool-estimate-target');
    fireEvent.change(targetInput, { target: { value: '5' } });
    fireEvent.click(screen.getByTestId('pool-estimate-run'));

    await waitFor(() => {
      expect(authGet).toHaveBeenCalledWith(
        expect.stringContaining(
          '/api/admin/projects/proj-estimate-1/pool-hit-rate-estimate?target_size=5',
        ),
      );
    });
    const hitRate = await screen.findByTestId('pool-estimate-hit-rate');
    expect(hitRate.textContent).toBe('75%');
    expect(screen.getByTestId('pool-estimate-result').textContent).toContain('6/8');
  });

  test('hides the estimate control when no active VM carries a project_id', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(baseState), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByText('backlog reuse');
    expect(screen.queryByTestId('pool-hit-rate-estimate-row')).toBeNull();
  });

  test('shows the target as inherited when the project has no explicit entry', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithProjectVm), { status: 200 }),
    );
    (fetchWarmPoolTargets as ReturnType<typeof vi.fn>).mockResolvedValue({
      warm_pool_target_size: 3,
      warm_pool_target_size_by_project: {},
    });
    render(<WarmVmPanel onViewTask={() => {}} />);

    const current = await screen.findByTestId('pool-target-current');
    expect(current.textContent).toContain('inherited');
    expect(current.textContent).toContain('3');
    // No explicit entry → no clear affordance.
    expect(screen.queryByTestId('pool-target-clear')).toBeNull();
  });

  test('shows the target as explicit when the project has a per-project entry', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithProjectVm), { status: 200 }),
    );
    (fetchWarmPoolTargets as ReturnType<typeof vi.fn>).mockResolvedValue({
      warm_pool_target_size: 3,
      warm_pool_target_size_by_project: { 'proj-estimate-1': 7 },
    });
    render(<WarmVmPanel onViewTask={() => {}} />);

    const current = await screen.findByTestId('pool-target-current');
    expect(current.textContent).toContain('explicit');
    expect(current.textContent).toContain('7');
    // Explicit entry → a clear affordance is offered.
    expect(screen.getByTestId('pool-target-clear')).toBeTruthy();
  });

  test('Set target posts the tuned N for the scoped project and refreshes', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithProjectVm), { status: 200 }),
    );
    (fetchWarmPoolTargets as ReturnType<typeof vi.fn>).mockResolvedValue({
      warm_pool_target_size: 2,
      warm_pool_target_size_by_project: {},
    });
    (setWarmPoolTargetForProject as ReturnType<typeof vi.fn>).mockResolvedValue({});
    render(<WarmVmPanel onViewTask={() => {}} />);

    const targetInput = await screen.findByTestId('pool-estimate-target');
    fireEvent.change(targetInput, { target: { value: '4' } });
    fireEvent.click(screen.getByTestId('pool-target-set'));

    await waitFor(() => {
      expect(setWarmPoolTargetForProject).toHaveBeenCalledWith('proj-estimate-1', 4);
    });
    // A successful set refetches state (initial load + post-set reload).
    await waitFor(() => {
      expect((authGet as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  test('Clear posts null to fall the project back to the global default', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateWithProjectVm), { status: 200 }),
    );
    (fetchWarmPoolTargets as ReturnType<typeof vi.fn>).mockResolvedValue({
      warm_pool_target_size: 2,
      warm_pool_target_size_by_project: { 'proj-estimate-1': 7 },
    });
    (setWarmPoolTargetForProject as ReturnType<typeof vi.fn>).mockResolvedValue({});
    render(<WarmVmPanel onViewTask={() => {}} />);

    fireEvent.click(await screen.findByTestId('pool-target-clear'));

    await waitFor(() => {
      expect(setWarmPoolTargetForProject).toHaveBeenCalledWith('proj-estimate-1', null);
    });
  });
});

describe('WarmVmPanel — per-project warm-pool scope control', () => {
  const projects: Project[] = [
    {
      id: 'proj-a', name: 'Project Alpha', status: 'active', github_owner: 'o',
      github_repo: 'alpha', autonomous_enabled: true, created_at: '2026-01-01',
    },
    {
      id: 'proj-b', name: 'Project Beta', status: 'active', github_owner: 'o',
      github_repo: 'beta', autonomous_enabled: true, created_at: '2026-01-01',
    },
  ];

  const stateScoped: WarmVmStateResponse = {
    ...baseState,
    pool_metrics: {
      enabled: true,
      target_size: 2,
      current_size: 1,
      idle_count: 1,
      project_allowlist: ['proj-a'],
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateScoped), { status: 200 }),
    );
    (enableWarmPoolForProject as ReturnType<typeof vi.fn>).mockResolvedValue({});
    (disableWarmPoolForProject as ReturnType<typeof vi.fn>).mockResolvedValue({});
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('does not render the scope row when no projects are passed', async () => {
    render(<WarmVmPanel onViewTask={() => {}} />);
    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('warm-pool-scope-row')).toBeNull();
  });

  test('renders scoped projects from the allowlist and a picker of candidates', async () => {
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    await screen.findByTestId('warm-pool-scope-row');
    // proj-a is scoped → shows by name with a disable button.
    const scoped = screen.getByTestId('warm-pool-scoped-proj-a');
    expect(scoped.textContent).toContain('Project Alpha');
    expect(screen.getByTestId('warm-pool-disable-proj-a')).toBeTruthy();
    // proj-b is NOT scoped → available in the enable picker only.
    const select = screen.getByTestId('warm-pool-scope-select') as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain('proj-b');
    expect(optionValues).not.toContain('proj-a');
  });

  test('Enable calls enableWarmPoolForProject for the selected project and refetches', async () => {
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    await screen.findByTestId('warm-pool-scope-row');
    const select = screen.getByTestId('warm-pool-scope-select');
    fireEvent.change(select, { target: { value: 'proj-b' } });
    fireEvent.click(screen.getByTestId('warm-pool-enable-btn'));

    await waitFor(() => {
      expect(enableWarmPoolForProject).toHaveBeenCalledWith('proj-b');
    });
    // Refetches state after the mutation (initial load + post-enable reload).
    await waitFor(() => {
      expect((authGet as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  test('Disable calls disableWarmPoolForProject for the scoped project', async () => {
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    await screen.findByTestId('warm-pool-scope-row');
    fireEvent.click(screen.getByTestId('warm-pool-disable-proj-a'));

    await waitFor(() => {
      expect(disableWarmPoolForProject).toHaveBeenCalledWith('proj-a');
    });
  });
});

describe('WarmVmPanel — per-project warm-pool payoff verdict', () => {
  const projects: Project[] = [
    {
      id: 'proj-a', name: 'Project Alpha', status: 'active', github_owner: 'o',
      github_repo: 'alpha', autonomous_enabled: true, created_at: '2026-01-01',
    },
  ];

  const stateScoped: WarmVmStateResponse = {
    ...baseState,
    pool_metrics: {
      enabled: true,
      target_size: 2,
      current_size: 1,
      idle_count: 1,
      project_allowlist: ['proj-a'],
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateScoped), { status: 200 }),
    );
    axesCell.value = { axes: {} };
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    axesCell.value = { axes: {} };
    cleanup();
    vi.restoreAllMocks();
  });

  test('renders the PROVEN badge, supporting numbers, and recommended action', async () => {
    axesCell.value = {
      axes: {},
      pool_payoff_verdict: {
        state: 'PROVEN',
        supporting: {
          pool_origin_share: 0.5,
          pool_warm_starts: 8,
          pool_hit_rate: 0.4,
          total_pool_dispatches: 50,
          cost_drift_pct: -2.0,
          cost_incl_idle_current: 10,
          cost_baseline: 10.2,
        },
        recommended_action: 'Keep the pool — consider widening scope.',
      },
    };
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    const badge = await screen.findByTestId('pool-payoff-badge-proj-a');
    expect(badge.textContent).toBe('PROVEN');
    const numbers = screen.getByTestId('pool-payoff-numbers-proj-a');
    expect(numbers.textContent).toContain('40%'); // hit rate
    expect(numbers.textContent).toContain('50%'); // pool-origin share
    expect(numbers.textContent).toContain('-2.0%'); // cost drift
    expect(screen.getByTestId('pool-payoff-action-proj-a').textContent).toContain(
      'widening scope',
    );
    expect(fetchEfficiencyAxes).toHaveBeenCalledWith('proj-a');
  });

  test('renders the COST_REGRESSION badge for a regressed project', async () => {
    axesCell.value = {
      axes: {},
      pool_payoff_verdict: {
        state: 'COST_REGRESSION',
        supporting: {
          pool_origin_share: 0.5,
          pool_warm_starts: 8,
          pool_hit_rate: 0.4,
          total_pool_dispatches: 50,
          cost_drift_pct: 20.0,
          cost_incl_idle_current: 12,
          cost_baseline: 10,
        },
        recommended_action: 'Reduce the pool target or disable.',
      },
    };
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    const badge = await screen.findByTestId('pool-payoff-badge-proj-a');
    expect(badge.textContent).toBe('COST REGRESSION');
    expect(screen.getByTestId('pool-payoff-numbers-proj-a').textContent).toContain('+20.0%');
  });

  test('does not render the verdict row when the backend omits the field (older backend)', async () => {
    axesCell.value = { axes: {} }; // no pool_payoff_verdict
    render(<WarmVmPanel onViewTask={() => {}} projects={projects} />);

    await screen.findByTestId('warm-pool-scope-row');
    expect(screen.queryByTestId('pool-payoff-verdict-row')).toBeNull();
  });
});

describe('WarmVmPanel — account-retarget callout', () => {
  // Extracted so tests can spread without non-null assertions.
  const retargetFunnel = {
    window_hours: 24,
    pool_lease_hits: 0,
    cold_dispatches: 8,
    total_dispatches: 8,
    pool_hit_rate: 0 as number | null,
    lease_miss_by_reason: { no_compatible_member: 8 } as Record<string, number>,
    lease_miss_total: 8,
  };
  const retargetPoolMetrics = {
    enabled: true,
    target_size: 2,
    current_size: 2,
    idle_count: 2,
    funnel: retargetFunnel,
  };
  // Pool enabled, low hit rate, no_compatible_member misses, retarget OFF.
  const stateRetargetNeeded: WarmVmStateResponse = {
    ...baseState,
    account_retarget_enabled: false,
    pool_metrics: retargetPoolMetrics,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (apiFetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    localStorage.setItem('blueprint-admin:warmVmPanelCollapsed', 'false');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test('renders callout when pool enabled, low hit rate, no_compatible_member misses, retarget off', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateRetargetNeeded), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const callout = await screen.findByTestId('account-retarget-callout');
    expect(callout.textContent).toContain('8');
    expect(callout.textContent).toContain('Account Retarget');
    expect(screen.getByTestId('account-retarget-enable-btn')).toBeTruthy();
  });

  test('does not render callout when account_retarget_enabled is true', async () => {
    const stateRetargetOn: WarmVmStateResponse = {
      ...stateRetargetNeeded,
      account_retarget_enabled: true,
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateRetargetOn), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('account-retarget-callout')).toBeNull();
  });

  test('does not render callout when no_compatible_member count is zero', async () => {
    const stateNoMismatch: WarmVmStateResponse = {
      ...stateRetargetNeeded,
      pool_metrics: {
        ...retargetPoolMetrics,
        funnel: {
          ...retargetFunnel,
          lease_miss_by_reason: { no_idle_member: 8 },
        },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateNoMismatch), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('account-retarget-callout')).toBeNull();
  });

  test('does not render callout when pool is disabled', async () => {
    const statePoolOff: WarmVmStateResponse = {
      ...stateRetargetNeeded,
      pool_metrics: { ...retargetPoolMetrics, enabled: false },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(statePoolOff), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('account-retarget-callout')).toBeNull();
  });

  test('does not render callout when hit rate is high (>= 0.5)', async () => {
    const stateHighHitRate: WarmVmStateResponse = {
      ...stateRetargetNeeded,
      pool_metrics: {
        ...retargetPoolMetrics,
        funnel: { ...retargetFunnel, pool_hit_rate: 0.6 },
      },
    };
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateHighHitRate), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    await screen.findByTestId('pool-metrics-row');
    expect(screen.queryByTestId('account-retarget-callout')).toBeNull();
  });

  test('Enable Account Retarget button calls flipFlag with warm_pool_account_retarget_enabled', async () => {
    (authGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify(stateRetargetNeeded), { status: 200 }),
    );
    render(<WarmVmPanel onViewTask={() => {}} />);

    const btn = await screen.findByTestId('account-retarget-enable-btn');
    fireEvent.click(btn);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        'http://localhost:8000/api/admin/settings',
        expect.objectContaining({ method: 'PATCH' }),
      );
    });
    const [, opts] = (apiFetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(opts.body as string);
    // Flips ONLY the retarget flag — must not touch sibling flags.
    expect(body).toEqual({ warm_pool_account_retarget_enabled: true });
  });
});
