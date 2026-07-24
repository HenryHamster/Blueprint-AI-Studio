import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { PoolAccountHealthPanel, type AccountHealthEntry } from './PoolAccountHealthPanel';

function makeAccount(overrides: Partial<AccountHealthEntry> = {}): AccountHealthEntry {
  return {
    account_id: 'acct-1',
    label: 'Primary Anthropic Account',
    provider: 'anthropic',
    state: 'healthy',
    health_score: 0.92,
    success_rate_7d: 0.88,
    completions_24h: 47,
    rl_events_24h: 2,
    rate_limited_models: [],
    failure_class_counts: { CODE_ERROR: 3, RATE_LIMIT: 1 },
    failure_class_buckets: { provider: 1, code: 3, infrastructure: 0 },
    ...overrides,
  };
}

describe('PoolAccountHealthPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    const accounts: AccountHealthEntry[] = [
      makeAccount({ account_id: 'acct-1', state: 'healthy' }),
      makeAccount({
        account_id: 'acct-2',
        label: 'Secondary Account',
        state: 'disqualified',
        health_score: 0.3,
        disabled_reason: 'Quota exhausted for claude-opus-4-5',
        rate_limited_models: [
          { model: 'claude-opus-4-5', cooldown_seconds_remaining: 3600, cooldown_type: 'quota_exhausted' },
        ],
      }),
    ];
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <PoolAccountHealthPanel accounts={accounts} loading={false} />,
        );
      });
    });
  });
});
