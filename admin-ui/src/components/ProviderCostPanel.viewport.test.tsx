import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { ProviderCostPanel } from './ProviderCostPanel';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({
        window_hours: 24,
        rows: [
          {
            provider: 'anthropic',
            account_id: 'acct-1',
            account_name: 'Primary Account',
            total_tasks: 120,
            success_count: 115,
            merged_count: 98,
            total_cost_usd: 42.75,
            success_rate: 0.958,
            cost_per_merged_pr: 0.44,
          },
          {
            provider: 'openai',
            account_id: 'acct-2',
            account_name: 'OpenAI Fallback',
            total_tasks: 18,
            success_count: 14,
            merged_count: 10,
            total_cost_usd: 8.30,
            success_rate: 0.778,
            cost_per_merged_pr: 0.83,
          },
        ],
      }),
      { status: 200 },
    ),
  ),
}));

describe('ProviderCostPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    const doRender = async () => {
      if (!host) {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        root = createRoot(host);
      }
      await act(async () => {
        root!.render(
          <ProviderCostPanel selectedProjectId={null} windowHours={24} />,
        );
      });
      // Wait for the mocked apiFetch to resolve and state to settle
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0));
      });
    };

    await expectNoOverflowAt(375, doRender);
    // Verify mobile card layout renders without silently clipping text
    await expectNoTextTruncationAt(375, async () => {});
  });
});
