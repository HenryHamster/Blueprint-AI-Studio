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

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response(JSON.stringify(baseState), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
}));

// useEfficiencySnapshot hits the network on mount; stub it out so the panel
// renders deterministically without an efficiency row.
vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: () => null,
  fmtEffSecs: () => '—',
  fmtEffRate: () => '—',
  fmtEffIter: () => '—',
}));

import { apiFetch, authGet } from '../api';

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
