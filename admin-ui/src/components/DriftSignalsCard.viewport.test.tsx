import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DriftSignalsCard } from './DriftSignalsCard';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/drift-snapshot')) {
      return new Response(
        JSON.stringify({
          flagged_directions: [
            {
              direction_id: 'dir-claim',
              direction_text:
                'Establish observability for autonomous loop self-diagnosis with sustained signal verification',
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 14,
              actually_merged_prs: 4,
              claim_shipped_consistency: 0.29,
              drift_history: [
                { snapshot_at: '2026-04-30T00:00:00Z', claim_shipped_consistency: 0.34, over_claim_flag: false, stalled_despite_churn_flag: false },
                { snapshot_at: '2026-05-02T00:00:00Z', claim_shipped_consistency: 0.31, over_claim_flag: true, stalled_despite_churn_flag: false },
                { snapshot_at: '2026-05-04T00:00:00Z', claim_shipped_consistency: 0.29, over_claim_flag: true, stalled_despite_churn_flag: false },
              ],
            },
            {
              direction_id: 'dir-stalled',
              direction_text: 'Mobile-responsive admin UI with viewport regression gate',
              over_claim_flag: false,
              stalled_despite_churn_flag: true,
              claimed_changes_count: 9,
              actually_merged_prs: 2,
              claim_shipped_consistency: 0.62,
              drift_history: [
                { snapshot_at: '2026-04-29T00:00:00Z', claim_shipped_consistency: 0.71, over_claim_flag: false, stalled_despite_churn_flag: false },
                { snapshot_at: '2026-05-03T00:00:00Z', claim_shipped_consistency: 0.62, over_claim_flag: false, stalled_despite_churn_flag: true },
              ],
            },
          ],
          flagged_count: 2,
          total_directions: 8,
          any_system_flagged: true,
          by_system: {
            'autonomous-research': {
              over_claim_flag: true,
              stalled_despite_churn_flag: false,
              claimed_changes_count: 22,
              actually_merged_prs: 6,
              claim_shipped_consistency: 0.27,
            },
            'admin-ui-mobile-viewport-gate': {
              over_claim_flag: false,
              stalled_despite_churn_flag: true,
              claimed_changes_count: 11,
              actually_merged_prs: 3,
              claim_shipped_consistency: 0.58,
            },
          },
          generated_at: '2026-05-08T12:00:00Z',
          window_days: 14,
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

describe('DriftSignalsCard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useRealTimers();
  });

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

  test('expanded card renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<DriftSignalsCard projectId="project-canary" />);
        });
        // Wait for fetch + state update.
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 80));
        });
        // Click the disclosure button to expand the directions list.
        const summaryBtn = document.querySelector<HTMLButtonElement>('button[aria-expanded]');
        if (summaryBtn) {
          await act(async () => {
            summaryBtn.click();
          });
        }
      },
      { waitMs: 50 },
    );
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(
      768,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<DriftSignalsCard projectId="project-canary" />);
        });
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 80));
        });
        const summaryBtn = document.querySelector<HTMLButtonElement>('button[aria-expanded]');
        if (summaryBtn) {
          await act(async () => {
            summaryBtn.click();
          });
        }
      },
      { waitMs: 50 },
    );
  });
});
