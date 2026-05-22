import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { WarmVmPanel } from './WarmVmPanel';
import type { WarmVmStateResponse } from '../types/warmVm';

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
});
