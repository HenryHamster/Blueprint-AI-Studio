import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, within } from '@testing-library/react';
import { ProviderCostPanel } from './ProviderCostPanel';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

// ProviderCostPanel also calls useEfficiencySnapshot, which issues its own
// apiFetch on a separate poll interval. Stub it so apiFetch call-count
// assertions reflect only the provider-cost-summary endpoint.
const mockUseEfficiencySnapshot = vi.fn<() => unknown>(() => null);
vi.mock('../hooks/useEfficiencySnapshot', () => ({
  useEfficiencySnapshot: () => mockUseEfficiencySnapshot(),
  fmtEffSecs: () => '—',
  fmtEffRate: () => '—',
  fmtEffIter: () => '—',
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

describe('ProviderCostPanel', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing before data arrives and when rows are empty', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ window_hours: 24, rows: [] }));

    const { container } = render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    expect(container.firstChild).toBeNull();

    await act(async () => {});
    expect(container.firstChild).toBeNull();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24', undefined);
  });

  it('renders table rows and appends project_id query parameter', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        window_hours: 24,
        rows: [
          {
            provider: 'openai',
            account_id: 'acc-1',
            account_name: 'Primary',
            total_tasks: 12,
            success_count: 10,
            merged_count: 4,
            total_cost_usd: 21.5,
            success_rate: 0.83,
            cost_per_merged_pr: 5.375,
          },
        ],
      }),
    );

    render(<ProviderCostPanel selectedProjectId="proj-abc" windowHours={24} />);

    expect(await screen.findByText('Provider Cost & Success')).toBeInTheDocument();
    // The panel renders both a mobile card layout (sm:hidden) and a desktop
    // table layout (hidden sm:table); jsdom applies no CSS so both are present.
    // Scope queries to the desktop table to avoid duplicate-text matches.
    const table = within(screen.getByRole('table'));
    expect(table.getByText('Primary')).toBeInTheDocument();
    expect(table.getByText('openai')).toBeInTheDocument();
    expect(table.getByText('83%')).toBeInTheDocument();
    expect(table.getByText('$21.50')).toBeInTheDocument();
    expect(table.getByText('$5.38')).toBeInTheDocument();
    expect(table.getByText('Watch cost')).toBeInTheDocument();
    const summary = screen.getByTestId('provider-cost-summary');
    expect(summary).toHaveTextContent('24h');
    expect(summary).toHaveTextContent('12');
    expect(summary).toHaveTextContent('4 merged');
    expect(summary).toHaveTextContent('$21.50');
    expect(screen.getByTestId('provider-cost-spend-pressure')).toHaveTextContent('Watch Primary: watch cost');
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24&project_id=proj-abc', undefined);
  });

  it('flags spend with no merged output as pressure', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        window_hours: 24,
        rows: [
          {
            provider: 'anthropic',
            account_id: 'acc-1',
            account_name: 'Expensive',
            total_tasks: 8,
            success_count: 2,
            merged_count: 0,
            total_cost_usd: 31.25,
            success_rate: 0.25,
            cost_per_merged_pr: null,
          },
        ],
      }),
    );

    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);

    expect(await screen.findByTestId('provider-cost-spend-pressure')).toHaveTextContent(
      'Reroute or inspect Expensive: no merges for spend',
    );
    expect(within(screen.getByRole('table')).getByText('No merges for spend')).toBeInTheDocument();
  });

  it('polls every 30 seconds', async () => {
    vi.useFakeTimers();
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        window_hours: 24,
        rows: [
          {
            provider: 'openai',
            account_id: null,
            account_name: 'Default',
            total_tasks: 1,
            success_count: 1,
            merged_count: 1,
            total_cost_usd: 1,
            success_rate: 1,
            cost_per_merged_pr: 1,
          },
        ],
      }),
    );

    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});
    expect(apiFetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });

  it('re-fetches when windowHours prop changes', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ window_hours: 24, rows: [] }));

    const { rerender } = render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    rerender(<ProviderCostPanel selectedProjectId={null} windowHours={168} />);
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24', undefined);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=168', undefined);
  });

  it('marks rows stale when polling fails after a successful load', async () => {
    const costRow = {
      provider: 'openai',
      account_id: null,
      account_name: 'Default',
      total_tasks: 5,
      success_count: 4,
      merged_count: 3,
      total_cost_usd: 10,
      success_rate: 0.8,
      cost_per_merged_pr: 3.33,
    };
    apiFetchMock.mockResolvedValueOnce(jsonResponse({ window_hours: 24, rows: [costRow] }));
    apiFetchMock.mockRejectedValue(new Error('network error'));

    vi.useFakeTimers();
    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);

    // Initial load succeeds — no stale banner yet
    await act(async () => {});
    // The panel renders both mobile card and desktop table layouts, so both contain 'Default'
    expect(screen.getAllByText('Default').length).toBeGreaterThan(0);
    expect(screen.queryByText(/stale/i)).toBeNull();

    // Next poll fails
    await act(async () => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});

    // Stale banner visible; rows still rendered
    expect(screen.getByText(/stale/i)).toBeInTheDocument();
    expect(screen.getAllByText('Default').length).toBeGreaterThan(0);
  });

  it('renders error placeholder when poll fails before any data', async () => {
    apiFetchMock.mockRejectedValue(new Error('network error'));

    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    expect(screen.getByText('Provider Cost & Success')).toBeInTheDocument();
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
  });

  it('clears stale banner once a poll succeeds again', async () => {
    const costRow = {
      provider: 'openai',
      account_id: null,
      account_name: 'Default',
      total_tasks: 5,
      success_count: 4,
      merged_count: 3,
      total_cost_usd: 10,
      success_rate: 0.8,
      cost_per_merged_pr: 3.33,
    };
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ window_hours: 24, rows: [costRow] }))
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValue(jsonResponse({ window_hours: 24, rows: [costRow] }));

    vi.useFakeTimers();
    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    await act(async () => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});
    expect(screen.getByText(/stale/i)).toBeInTheDocument();

    await act(async () => { vi.advanceTimersByTime(30_000); });
    await act(async () => {});
    // Banner cleared after successful recovery poll
    expect(screen.queryByText(/stale/i)).toBeNull();
  });
});

describe('ProviderCostPanel — warm-start origin breakdown', () => {
  const costRow = {
    provider: 'openai',
    account_id: null,
    account_name: 'Default',
    total_tasks: 5,
    success_count: 4,
    merged_count: 3,
    total_cost_usd: 10,
    success_rate: 0.8,
    cost_per_merged_pr: 3.33,
  };

  const efficiencySnapshot = {
    computed_at: new Date().toISOString(),
    window_24h: {
      setup_p50_secs: null,
      setup_p95_secs: null,
      setup_sample_n: 0,
      warm_reuse_rate: 0.5,
      warm_reuse_sample_n: 8,
      review_iter_median: null,
      review_iter_p95: null,
      review_iter_sample_n: 0,
      vs_prior_setup_p50_delta: null,
      vs_prior_warm_reuse_rate_delta: null,
      vs_prior_review_iter_median_delta: null,
      warm_start_origin_breakdown: {
        warm_pool: 2,
        backlog_reuse: 1,
        chain_continuity: 1,
        warm_total: 4,
        pool_origin_share: 0.5,
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
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue(jsonResponse({ window_hours: 24, rows: [costRow] }));
  });

  afterEach(() => {
    mockUseEfficiencySnapshot.mockReturnValue(null);
  });

  it('renders warm-start origin breakdown alongside the aggregate reuse rate', async () => {
    mockUseEfficiencySnapshot.mockReturnValue(efficiencySnapshot);
    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    const row = screen.getByTestId('warm-start-origin-breakdown');
    expect(row.textContent).toContain('warm by origin');
    expect(row.textContent).toContain('2');
    expect(row.textContent).toContain('1');
    expect(screen.getByTestId('warm-start-pool-share').textContent).toContain('50%');
  });

  it('does not render breakdown row when warm_start_origin_breakdown is absent', async () => {
    mockUseEfficiencySnapshot.mockReturnValue({
      ...efficiencySnapshot,
      window_24h: { ...efficiencySnapshot.window_24h, warm_start_origin_breakdown: undefined },
    });
    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    expect(screen.queryByTestId('warm-start-origin-breakdown')).toBeNull();
  });
});
