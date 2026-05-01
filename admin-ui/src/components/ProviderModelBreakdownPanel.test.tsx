import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
}));

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

function makeRow(overrides: Record<string, unknown> = {}) {
  return {
    provider: 'anthropic',
    model: 'claude-opus-4-7',
    total_tasks: 20,
    success_count: 16,
    success_rate: 0.8,
    distinct_accounts: 2,
    project_median_success_rate: 0.8,
    accounts: [],
    ...overrides,
  };
}

function makeAccount(overrides: Record<string, unknown> = {}) {
  return {
    account_id: 'acct-1',
    total_tasks: 10,
    success_count: 8,
    success_rate: 0.8,
    below_median: false,
    quarantine_status: null,
    ...overrides,
  };
}

describe('ProviderModelBreakdownPanel', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('renders nothing when rows array is empty', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [] }));

    const { container } = render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});
    expect(container.firstChild).toBeNull();
  });

  it('renders top-level provider/model row with success rate color coding', async () => {
    const row = makeRow({ success_rate: 0.85, project_median_success_rate: 0.82 });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    expect(screen.getByText('claude-opus-4-7')).toBeInTheDocument();
    expect(screen.getByText('anthropic')).toBeInTheDocument();
    expect(screen.getAllByText('85%').length).toBeGreaterThan(0);
  });

  it('expanding a row reveals per-account drill-in with below_median badge', async () => {
    const row = makeRow({
      distinct_accounts: 2,
      accounts: [
        makeAccount({ account_id: 'acct-1', success_rate: 0.5, below_median: true }),
        makeAccount({ account_id: 'acct-2', success_rate: 0.9, below_median: false }),
      ],
    });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    // Per-account entries not visible before expand
    expect(screen.queryByText('below median', { exact: false })).not.toBeInTheDocument();

    // Click the row to expand
    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.getByText(/below median/i)).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText('90%')).toBeInTheDocument();
  });

  it('quarantine_status badge shows kind and rate vs median', async () => {
    const row = makeRow({
      accounts: [
        makeAccount({
          account_id: 'acct-1',
          below_median: true,
          quarantine_status: {
            kind: 'regression',
            started_at: '2026-04-01T10:00:00Z',
            account_rate: 0.45,
            project_median: 0.82,
            sample_size: 8,
          },
        }),
      ],
    });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.getByText(/quarantine:regression/i)).toBeInTheDocument();
    expect(screen.getByText(/45%.*82%/i)).toBeInTheDocument();
  });

  it('Clear quarantine button absent when quarantine_status is null', async () => {
    const row = makeRow({
      accounts: [makeAccount({ quarantine_status: null })],
    });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.queryByText(/clear quarantine/i)).not.toBeInTheDocument();
  });

  it('Clear quarantine button absent when kind is not regression', async () => {
    const row = makeRow({
      accounts: [
        makeAccount({
          quarantine_status: { kind: 'rate_limit', started_at: null, account_rate: null, project_median: null, sample_size: null },
        }),
      ],
    });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.queryByText(/clear quarantine/i)).not.toBeInTheDocument();
  });

  it('Clear quarantine button present for regression kind and sends POST on confirm', async () => {
    const row = makeRow({
      accounts: [
        makeAccount({
          account_id: 'acct-abc',
          quarantine_status: {
            kind: 'regression',
            started_at: null,
            account_rate: 0.4,
            project_median: 0.8,
            sample_size: 5,
          },
        }),
      ],
    });
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ rows: [row] }))
      .mockResolvedValue(jsonResponse({ cleared: true }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.getByText(/clear quarantine/i)).toBeInTheDocument();

    // Click to enter confirmation state
    await user.click(screen.getByText(/clear quarantine/i));
    expect(screen.getByText(/confirm clear/i)).toBeInTheDocument();

    // Confirm
    await act(async () => {
      await user.click(screen.getByText(/confirm clear/i));
    });

    const calls = apiFetchMock.mock.calls;
    const postCall = calls.find(
      ([url, opts]: [string, RequestInit]) =>
        url.includes('acct-abc') &&
        url.includes('regression-quarantine/clear') &&
        (opts as RequestInit)?.method === 'POST',
    );
    expect(postCall).toBeDefined();
  });

  it('uses account label from healthAccounts when provided', async () => {
    const row = makeRow({
      accounts: [makeAccount({ account_id: 'acct-xyz' })],
    });
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [row] }));

    const user = userEvent.setup();
    render(
      <ProviderModelBreakdownPanel
        selectedProjectId={null}
        accounts={[
          {
            account_id: 'acct-xyz',
            label: 'My Fancy Account',
            provider: 'anthropic',
            state: 'healthy',
            health_score: 0.9,
            completions_24h: 10,
            rl_events_24h: 0,
            rate_limited_models: [],
            failure_class_counts: {},
          },
        ]}
      />,
    );
    await act(async () => {});

    await user.click(screen.getByText('claude-opus-4-7'));
    expect(screen.getByText('My Fancy Account')).toBeInTheDocument();
  });

  it('hide single-account toggle filters out rows with distinct_accounts < 2', async () => {
    const rows = [
      makeRow({ model: 'model-a', distinct_accounts: 1 }),
      makeRow({ model: 'model-b', distinct_accounts: 3 }),
    ];
    apiFetchMock.mockResolvedValue(jsonResponse({ rows }));

    const user = userEvent.setup();
    render(<ProviderModelBreakdownPanel selectedProjectId={null} />);
    await act(async () => {});

    expect(screen.getByText('model-a')).toBeInTheDocument();
    expect(screen.getByText('model-b')).toBeInTheDocument();

    await user.click(screen.getByText('Hide single-account'));
    expect(screen.queryByText('model-a')).not.toBeInTheDocument();
    expect(screen.getByText('model-b')).toBeInTheDocument();
  });
});
