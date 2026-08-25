import { afterEach, describe, test, vi, beforeEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { LaneDiagPanel } from './LaneDiagPanel';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

const mockDiagResponse = {
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
    waste_task_count: 24,
    cap_counted_task_count: 24,
    cap_excluded_task_count: 0,
    chain_count: 10,
    cap_exhausted_chain_count: 0,
    chain_limit: 50,
    chains_truncated: false,
  },
  checkpoint_recovery_waste: {
    by_reason: { stale_context: 4, branch_reset: 2 },
    total: 6,
    exhausted_count: 0,
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
};

const mockBaselinesResponse = {
  project_id: 'proj-1',
  count: 2,
  baselines: [],
  trend: {
    latest_version: 10,
    prior_version: 9,
    relay_medians: {
      wall_clock_seconds: { latest: 3600, prior: 4200, delta: -600, classification: 'improving' },
      active_seconds: { latest: 2400, prior: 2800, delta: -400, classification: 'improving' },
      gap_seconds: { latest: 300, prior: 350, delta: -50, classification: 'stable' },
      task_count: { latest: 3.2, prior: 3.8, delta: -0.6, classification: 'improving' },
    },
    lane_iterations: {
      conflict: {
        p50: { latest: 1.5, prior: 2.0, delta: -0.5, classification: 'stable' },
        p90: { latest: 3.0, prior: 4.0, delta: -1.0, classification: 'improving' },
      },
      ci: {
        p50: { latest: 1.2, prior: 1.5, delta: -0.3, classification: 'stable' },
        p90: { latest: 2.5, prior: 3.0, delta: -0.5, classification: 'stable' },
      },
      review_convergence: {
        p50: { latest: 1.0, prior: 1.5, delta: -0.5, classification: 'stable' },
        p90: { latest: 2.0, prior: 3.0, delta: -1.0, classification: 'improving' },
      },
    },
    waste_taxonomy_shares: {
      infra: { latest: 0.1, prior: 0.15, delta: -0.05, classification: 'stable' },
      merge_conflict: { latest: 0.05, prior: 0.1, delta: -0.05, classification: 'stable' },
      ci_churn: { latest: 0.2, prior: 0.3, delta: -0.1, classification: 'improving' },
      review_ambiguity: { latest: 0.1, prior: 0.05, delta: 0.05, classification: 'stable' },
      lost_reasoning: { latest: 0.05, prior: 0.1, delta: -0.05, classification: 'stable' },
      unknown: { latest: 0.1, prior: 0.1, delta: 0, classification: 'stable' },
    },
    checkpoint_recovery_waste_total: {
      latest: 6.0,
      prior: 10.0,
      delta: -4.0,
      classification: 'improving',
    },
    outer_loop_convergence: {
      iteration_median: { latest: 2, prior: 4, delta: -2, classification: 'improving' },
      iteration_p95: { latest: 3, prior: 5, delta: -2, classification: 'improving' },
      convergence_rate: { latest: 1, prior: 0.5, delta: 0.5, classification: 'improving' },
      cap_exhausted_chain_count: { latest: 0, prior: 1, delta: -1, classification: 'improving' },
      cap_exhausted_direction_count: { latest: 0, prior: 1, delta: -1, classification: 'improving' },
      cap_exhausted_directions: {
        latest: [{ direction_id: 'd-latest', direction_text: 'Latest cap-loss direction' }],
        prior: [{ direction_id: 'd-prior', direction_text: 'Prior cap-loss direction' }],
      },
    },
  },
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(JSON.stringify(mockDiagResponse), { status: 200 }),
  ),
  fetchLaneBaselines: vi.fn(async () => mockBaselinesResponse),
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

  test('renders checkpoint-recovery-waste section at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
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

  test('renders cross-version trend section at 1280px', async () => {
    await expectNoTextTruncationAt(1280, async () => {
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
});
