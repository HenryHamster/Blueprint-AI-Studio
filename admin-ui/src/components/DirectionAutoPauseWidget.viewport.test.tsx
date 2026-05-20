import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DirectionAutoPauseWidget } from './DirectionAutoPauseWidget';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/direction-auto-pause-stats')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          auto_paused_count: 14,
          auto_recovered_count: 8,
          auto_retired_count: 3,
          currently_auto_paused: 2,
          trend: 'up',
          top_reasons: [
            {
              reason: 'consecutive CODE_ERROR remediation failures with identical fingerprint over a 6-hour window',
              count: 6,
            },
            { reason: 'cost-per-merge exceeded threshold ($12.40 / merged PR)', count: 4 },
            { reason: 'over_claim_flag — claimed_changes 14 vs actually_merged 2', count: 2 },
            { reason: 'sustained zero-merge cycles (3 cycles, 0 merges)', count: 1 },
            { reason: 'manual operator pause via /api/admin/directions endpoint', count: 1 },
          ],
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

describe('DirectionAutoPauseWidget — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionAutoPauseWidget selectedProjectId="project-canary" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DirectionAutoPauseWidget selectedProjectId="project-canary" />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });
});
