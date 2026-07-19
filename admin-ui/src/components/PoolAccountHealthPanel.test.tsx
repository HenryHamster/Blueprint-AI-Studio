import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// firebase.ts calls getAuth() at module load; stub it out so tests don't need env vars
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
}));

import { PoolAccountHealthPanel } from './PoolAccountHealthPanel';
import type { AccountHealthEntry } from './PoolAccountHealthPanel';

function makeAccount(overrides: Partial<AccountHealthEntry> = {}): AccountHealthEntry {
  return {
    account_id: 'acct-1',
    label: 'Test Account',
    provider: 'anthropic',
    state: 'healthy',
    health_score: 0.9,
    completions_24h: 50,
    rl_events_24h: 0,
    rate_limited_models: [],
    failure_class_counts: {},
    ...overrides,
  };
}

describe('PoolAccountHealthPanel', () => {
  it('renders multi-class breakdown with dominant class label and 7d success rate', () => {
    const account = makeAccount({
      failure_class_counts: { CODE_ERROR: 5, RATE_LIMIT: 2 },
      success_rate_7d: 0.61,
      health_score: 0.6,
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    expect(screen.getByText('Provider Account Health')).toBeInTheDocument();
    expect(screen.getByTitle('Dominant failure class (24h)')).toHaveTextContent('code:5');
    expect(screen.getByTitle('7-day success rate')).toHaveTextContent('61% 7d');
  });

  it('renders healthy account with green 7d rate and no failure bars', () => {
    const account = makeAccount({
      failure_class_counts: {},
      success_rate_7d: 0.95,
      health_score: 0.95,
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    const sr7dEl = screen.getByTitle('7-day success rate');
    expect(sr7dEl).toHaveTextContent('95% 7d');
    expect(sr7dEl.className).toContain('text-emerald-400');
    expect(screen.queryByTitle('Dominant failure class (24h)')).not.toBeInTheDocument();
  });

  it('renders a compact state, cooldown, and usage summary', () => {
    render(
      <PoolAccountHealthPanel
        accounts={[
          makeAccount({
            account_id: 'healthy',
            usage_snapshot: {
              known: true,
              five_hour_utilization: 25,
              seven_day_utilization: 40,
              resets_at: {},
              probed_at: null,
              stale: false,
            },
          }),
          makeAccount({
            account_id: 'disabled',
            state: 'disabled',
            usage_snapshot: {
              known: true,
              five_hour_utilization: 92,
              seven_day_utilization: 82,
              resets_at: {},
              probed_at: null,
              stale: false,
            },
            rate_limited_models: [
              { model: 'claude-rate', cooldown_seconds_remaining: 120, cooldown_type: 'rate_limit' },
              { model: 'claude-regression', cooldown_seconds_remaining: 3600, cooldown_type: 'regression' },
            ],
          }),
        ]}
        loading={false}
      />,
    );

    const summary = screen.getByTestId('account-health-summary');
    expect(summary).toHaveTextContent('1/2');
    expect(summary).toHaveTextContent('2 model cooldowns');
    expect(summary).toHaveTextContent('1 regressions');
    expect(screen.getAllByTestId('account-health-next-action').map(el => el.textContent)).toEqual([
      'Next: Ready for dispatch',
      'Next: Disabled: enable or rotate credentials',
    ]);
  });

  it('prioritizes quota exhaustion and usage pressure as row next actions', () => {
    render(
      <PoolAccountHealthPanel
        accounts={[
          makeAccount({
            account_id: 'quota',
            rate_limited_models: [
              { model: 'claude-quota', cooldown_seconds_remaining: 3600, cooldown_type: 'quota_exhausted' },
            ],
            usage_snapshot: {
              known: true,
              five_hour_utilization: 95,
              seven_day_utilization: 40,
              resets_at: {},
              probed_at: null,
              stale: false,
            },
          }),
          makeAccount({
            account_id: 'pressure',
            usage_snapshot: {
              known: true,
              five_hour_utilization: 76,
              seven_day_utilization: 50,
              resets_at: {},
              probed_at: null,
              stale: false,
            },
          }),
        ]}
        loading={false}
      />,
    );

    const actions = screen.getAllByTestId('account-health-next-action').map(el => el.textContent);
    expect(actions).toContain('Next: Hold traffic: daily quota exhausted');
    expect(actions).toContain('Next: Usage pressure: keep as backup');
  });

  it('renders regression diagnostic line for regression-quarantined models', () => {
    const account = makeAccount({
      state: 'disabled',
      rate_limited_models: [
        {
          model: 'claude-opus-4-7',
          cooldown_type: 'regression',
          cooldown_seconds_remaining: 3600,
          regression_stats: {
            account_rate: 0.42,
            project_median: 0.78,
            sample_size: 25,
            distinct_accounts: 3,
          },
        },
      ],
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    const diag = screen.getByText(/regression on claude-opus-4-7/);
    expect(diag).toBeInTheDocument();
    expect(diag.textContent).toContain('42% vs project median 78%');
    expect(diag.textContent).toContain('n=25 over 3 accounts');
  });

  it('renders 5h/7d utilization badges for a known usage snapshot', () => {
    const account = makeAccount({
      usage_snapshot: {
        known: true,
        five_hour_utilization: 42,
        seven_day_utilization: 88,
        resets_at: { five_hour: '2026-06-06T18:00:00Z', seven_day: '2026-06-12T00:00:00Z' },
        probed_at: '2026-06-06T12:00:00Z',
        stale: false,
      },
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    const badge = screen.getByTitle(/Usage headroom — 5h \/ 7d window utilization/);
    expect(badge).toHaveTextContent('5h 42%');
    expect(badge).toHaveTextContent('7d 88%');
  });

  it('renders an explicit unknown state (not 0%) when usage is unknown', () => {
    const account = makeAccount({
      usage_snapshot: {
        known: false,
        five_hour_utilization: null,
        seven_day_utilization: null,
        resets_at: {},
        probed_at: null,
        stale: true,
      },
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    expect(screen.getByText('usage ?')).toBeInTheDocument();
    // Explicit unknown — no synthesized 5h/7d utilization badge.
    expect(screen.queryByText(/5h/)).not.toBeInTheDocument();
    expect(screen.queryByText(/7d/)).not.toBeInTheDocument();
  });

  it('renders no usage badge and does not throw when usage_snapshot is absent', () => {
    const account = makeAccount({});
    expect(() =>
      render(<PoolAccountHealthPanel accounts={[account]} loading={false} />),
    ).not.toThrow();
    expect(screen.queryByText('usage ?')).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Usage headroom/)).not.toBeInTheDocument();
  });

  it('shows reset times and probed_at in the expanded usage detail', () => {
    const account = makeAccount({
      usage_snapshot: {
        known: true,
        five_hour_utilization: 10,
        seven_day_utilization: 20,
        resets_at: { five_hour: '2026-06-06T18:00:00Z', seven_day: '2026-06-12T00:00:00Z' },
        probed_at: '2026-06-06T12:00:00Z',
        stale: false,
      },
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    // Expand the row by clicking the account toggle button.
    fireEvent.click(screen.getByRole('button'));

    const detail = screen.getByTitle('Usage headroom detail');
    expect(detail).toHaveTextContent('resets');
    expect(detail).toHaveTextContent('probed');
  });

  it('applies reduced opacity for disabled and disqualified accounts', () => {
    const { rerender } = render(
      <PoolAccountHealthPanel
        accounts={[makeAccount({ state: 'disabled', account_id: 'a1' })]}
        loading={false}
      />,
    );
    expect(document.querySelector('.opacity-50')).not.toBeNull();

    rerender(
      <PoolAccountHealthPanel
        accounts={[makeAccount({ state: 'disqualified', account_id: 'a2' })]}
        loading={false}
      />,
    );
    expect(document.querySelector('.opacity-50')).not.toBeNull();

    rerender(
      <PoolAccountHealthPanel
        accounts={[makeAccount({ state: 'healthy', account_id: 'a3' })]}
        loading={false}
      />,
    );
    expect(document.querySelector('.opacity-50')).toBeNull();
  });

  it('stale usage snapshot renders dimmed with explicit stale label', () => {
    const account = makeAccount({
      usage_snapshot: {
        known: true,
        five_hour_utilization: 42,
        seven_day_utilization: 88,
        resets_at: { five_hour: '2026-06-06T18:00:00Z', seven_day: '2026-06-12T00:00:00Z' },
        probed_at: '2026-06-06T12:00:00Z',
        stale: true,
      },
    });
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    const badge = screen.getByTitle(/Usage headroom — 5h \/ 7d window utilization/);
    expect(badge).toHaveClass('opacity-60');
    expect(badge).toHaveTextContent('stale');
    expect(badge.textContent).toMatch(/stale @/);
  });

  it('renders a response-level stale chip when servedStale is true', () => {
    const account = makeAccount();
    render(
      <PoolAccountHealthPanel
        accounts={[account]}
        loading={false}
        servedStale
        generatedAt="2026-06-13T00:00:00Z"
      />,
    );

    // The response-level chip is distinct from the per-account usage_snapshot.stale
    // indicator (which this account does not carry).
    const chip = screen.getByTitle(/whole panel is a cached fallback/i);
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveTextContent(/stale/i);
  });

  it('renders no response-level stale chip when servedStale is absent/false', () => {
    const account = makeAccount();
    render(<PoolAccountHealthPanel accounts={[account]} loading={false} />);

    expect(screen.queryByTitle(/whole panel is a cached fallback/i)).not.toBeInTheDocument();
  });
});
