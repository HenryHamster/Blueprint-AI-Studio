import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';
import type { AccountHealthEntry } from './PoolAccountHealthPanel';

// The panel renders `null` until apiFetch resolves with a non-empty rows array,
// so we mock the API to return a populated breakdown that exercises both the
// table-row layout and the expanded-account sub-row (with quarantine badge).
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({
        rows: [
          {
            provider: 'anthropic',
            model: 'claude-opus-4-7',
            total_tasks: 248,
            success_count: 231,
            success_rate: 0.931,
            distinct_accounts: 3,
            project_median_success_rate: 0.94,
            accounts: [
              {
                account_id: 'acct-primary-1234',
                total_tasks: 120,
                success_count: 118,
                success_rate: 0.983,
                below_median: false,
                quarantine_status: null,
              },
              {
                account_id: 'acct-secondary-5678',
                total_tasks: 80,
                success_count: 70,
                success_rate: 0.875,
                below_median: true,
                quarantine_status: {
                  kind: 'regression',
                  started_at: '2026-05-07T16:30:00Z',
                  account_rate: 0.62,
                  project_median: 0.92,
                  sample_size: 24,
                },
              },
              {
                account_id: 'acct-overflow-9abc',
                total_tasks: 48,
                success_count: 43,
                success_rate: 0.896,
                below_median: false,
                quarantine_status: null,
              },
            ],
          },
          {
            provider: 'openai',
            model: 'gpt-5-codex',
            total_tasks: 56,
            success_count: 47,
            success_rate: 0.839,
            distinct_accounts: 2,
            project_median_success_rate: 0.88,
            accounts: [
              {
                account_id: 'acct-openai-aaaa',
                total_tasks: 40,
                success_count: 35,
                success_rate: 0.875,
                below_median: false,
                quarantine_status: null,
              },
              {
                account_id: 'acct-openai-bbbb',
                total_tasks: 16,
                success_count: 12,
                success_rate: 0.75,
                below_median: true,
                quarantine_status: null,
              },
            ],
          },
          {
            provider: 'gemini',
            model: 'gemini-2.5-pro',
            total_tasks: 18,
            success_count: 11,
            success_rate: 0.611,
            distinct_accounts: 1,
            project_median_success_rate: 0.85,
            accounts: [],
          },
        ],
      }),
      { status: 200 },
    ),
  ),
}));

const accounts: AccountHealthEntry[] = [
  {
    account_id: 'acct-primary-1234',
    label: 'Primary',
    provider: 'anthropic',
    state: 'healthy',
    health_score: 96,
    success_rate_7d: 0.95,
    completions_24h: 42,
    rl_events_24h: 0,
    rate_limited_models: [],
    failure_class_counts: {},
  },
  {
    account_id: 'acct-secondary-5678',
    label: 'Secondary Reserve',
    provider: 'anthropic',
    state: 'disqualified',
    health_score: 71,
    success_rate_7d: 0.78,
    completions_24h: 18,
    rl_events_24h: 2,
    rate_limited_models: [],
    failure_class_counts: { code: 4 },
  },
];

describe('ProviderModelBreakdownPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with populated rows', async () => {
    const doRender = async () => {
      if (!host) {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        root = createRoot(host);
      }
      await act(async () => {
        root!.render(
          <ProviderModelBreakdownPanel
            selectedProjectId="project-test"
            accounts={accounts}
          />,
        );
      });
      // Settle the mocked apiFetch + setRows() — without this, rows is still
      // empty and the component returns null, making the test trivially pass.
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0));
      });
    };

    await expectNoOverflowAt(375, doRender);
    // Verify mobile card layout renders without silently clipping text
    await expectNoTextTruncationAt(375, async () => {});
  });

  test('expanded account sub-cards render without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProviderModelBreakdownPanel
            selectedProjectId="project-test"
            accounts={accounts}
          />,
        );
      });
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0));
      });
      // Find the first cursor-pointer div (mobile card row) and click to expand
      const allDivs = Array.from(host!.querySelectorAll<HTMLElement>('div'));
      const firstClickable = allDivs.find(d => d.className.includes('cursor-pointer'));
      if (firstClickable) {
        await act(async () => { firstClickable.click(); });
      }
    });
    await expectNoTextTruncationAt(375, async () => {});
  });
});
