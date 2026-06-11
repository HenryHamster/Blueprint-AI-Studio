import { afterEach, describe, test, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { LaneDiagPanel } from './LaneDiagPanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({
        pr_count: 12,
        task_count: 48,
        window_days: 7,
        truncated: false,
        pr_breakdown_truncated: false,
        relay_counts_by_lane: {
          'merge-fix-long-lane-name-that-may-overflow': 5,
          'ci-repair': 3,
          'review-rework': 2,
        },
        lane_waste: {
          waste_taxonomy: {
            infra: 8,
            merge_conflict: 4,
            ci_churn: 6,
            review_ambiguity: 3,
            lost_reasoning: 2,
            unknown: 1,
          },
        },
        per_pr_phase_breakdown: [
          {
            pr_number: 9494,
            task_count: 4,
            review_seconds: 3600,
            fix_seconds: 7200,
            queue_seconds: 600,
            provision_seconds: 120,
            gap_seconds: 300,
            total_seconds: 11820,
          },
          {
            pr_number: 9491,
            task_count: 2,
            review_seconds: 1800,
            fix_seconds: 3600,
            queue_seconds: 300,
            provision_seconds: 90,
            gap_seconds: 150,
            total_seconds: 5940,
          },
        ],
      }),
      { status: 200 },
    ),
  ),
}));

describe('LaneDiagPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LaneDiagPanel projectId="proj-1" />);
      });
      // Wait for the async fetch to complete and state to settle.
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  });

  test('operator-facing text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LaneDiagPanel projectId="proj-1" />);
      });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<LaneDiagPanel projectId="proj-1" />);
        });
        await new Promise((resolve) => setTimeout(resolve, 50));
      },
      { selector: 'button, a[href]' },
    );
  });
});
