import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WarmVmPanel } from './WarmVmPanel';
import { expectNoOverflowAt } from '../test/viewport';
import type { WarmVmStateResponse } from '../types/warmVm';

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
  API_BASE_URL: 'http://localhost:8000',
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
