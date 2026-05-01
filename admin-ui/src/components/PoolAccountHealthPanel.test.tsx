import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// firebase.ts calls getAuth() at module load; stub it out so tests don't need env vars
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
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
});
