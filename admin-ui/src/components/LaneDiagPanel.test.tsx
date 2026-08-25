import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { act } from 'react';
import { LaneDiagPanel } from './LaneDiagPanel';
import type { LaneBaselinesResponse } from '../api/strategic';

const mockLaneDiagResponse = {
  pr_count: 5,
  task_count: 20,
  truncated: false,
  pr_breakdown_limit: 100,
  pr_breakdown_truncated: false,
  window_days: 7,
  window_start: '2026-04-17T12:00:00Z',
  project_id: 'project-1',
  max_tasks: 5000,
  relay_counts_by_lane: {
    LANE_CI: 3,
    LANE_CONFLICT: 1,
    LANE_REVIEW_CONVERGENCE: 2,
  },
  lane_iteration_distribution: {},
  per_pr_phase_breakdown: [
    {
      pr_number: '42',
      task_count: 5,
      review_seconds: 3600,
      fix_seconds: 7200,
      gap_seconds: 600,
      total_seconds: 11400,
      queue_seconds: 120,
      provision_seconds: 80,
      cold_start_seconds: 50,
      execution_seconds: 7150,
    },
    {
      pr_number: '41',
      task_count: 2,
      review_seconds: 1800,
      fix_seconds: 3600,
      gap_seconds: 300,
      total_seconds: 5700,
      queue_seconds: 60,
      provision_seconds: 40,
      cold_start_seconds: 30,
      execution_seconds: 3570,
    },
  ],
  lane_waste: {
    waste_taxonomy: {
      infra: 2,
      merge_conflict: 1,
      ci_churn: 3,
      review_ambiguity: 0,
      lost_reasoning: 1,
      unknown: 0,
    },
    waste_task_count: 7,
    cap_counted_task_count: 7,
    cap_excluded_task_count: 0,
    chain_count: 4,
    cap_exhausted_chain_count: 2,
    cap_exhausted_direction_count: 1,
    cap_exhausted_directions: [{ direction_id: 'd-cap', direction_text: 'Cap direction' }],
    supersede_affected_chain_count: 3,
    reviews_to_converge: { count: 3, median: 2, p95: 5 },
    supersede_affected_reviews_to_converge: { count: 2, sample_count: 2, median: 6, p95: 9 },
    rollup_chain_count: 3,
    rollup_supersede_affected_chain_count: 2,
    rollup_supersede_unaffected_chain_count: 1,
    rollup_supersede_affected_reviews_to_converge: { count: 2, sample_count: 2, median: 6, p95: 9 },
    rollup_supersede_unaffected_reviews_to_converge: { count: 1, sample_count: 1, median: 2, p95: 3 },
    retarget_review_scope_counts: { delta: 1, full: 2 },
    stale_head_discard_count: 0,
    chain_limit: 50,
    chains_truncated: false,
    chains: [],
  },
  checkpoint_recovery_waste: {
    by_reason: { stale_context: 4, branch_reset: 2 },
    total: 6,
    exhausted_count: 0,
  },
};

const mockBaselinesResponse: LaneBaselinesResponse = {
  project_id: 'project-1',
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
      cap_exhausted_direction_count: { latest: 1, prior: 3, delta: -2, classification: 'improving' },
      cap_exhausted_directions: {
        latest: [
          { direction_id: 'd-latest', direction_text: 'Latest cap loss' },
          { direction_id: 'd-id-only', direction_text: null },
        ],
        prior: [{ direction_id: 'd-prior', direction_text: 'Prior cap loss' }],
      },
      supersede_affected_reviews_to_converge_median: {
        latest: 6, prior: 4, delta: 2, classification: 'declining',
      },
      supersede_affected_reviews_to_converge_p95: {
        latest: 9, prior: 7, delta: 2, classification: 'declining',
      },
      rollup_supersede_affected_reviews_to_converge_median: {
        latest: 6, prior: 4, delta: 2, classification: 'declining',
      },
      rollup_supersede_affected_reviews_to_converge_p95: {
        latest: 9, prior: 7, delta: 2, classification: 'declining',
      },
      rollup_supersede_unaffected_reviews_to_converge_median: {
        latest: 2, prior: 3, delta: -1, classification: 'improving',
      },
      rollup_supersede_unaffected_reviews_to_converge_p95: {
        latest: 3, prior: 4, delta: -1, classification: 'improving',
      },
    },
    retarget_convergence: {
      reviews_to_converge: {
        // Sample size, not cost: the backend leaves this row unclassified.
        count: { latest: 3, prior: 4, delta: -1, classification: null },
        median: { latest: 2, prior: 4, delta: -2, classification: 'improving' },
        p95: { latest: 5, prior: 7, delta: -2, classification: 'improving' },
      },
      retarget_review_scope_counts: {
        delta: { latest: 1, prior: 3, delta: -2, classification: 'improving' },
        full: { latest: 2, prior: 1, delta: 1, classification: 'declining' },
      },
      stale_head_discard_count: { latest: 0, prior: 2, delta: -2, classification: 'improving' },
    },
  },
};

const { apiFetch, fetchLaneBaselines } = vi.hoisted(() => ({
  apiFetch: vi.fn(async () => {
    return new Response(JSON.stringify(mockLaneDiagResponse), { status: 200 });
  }),
  fetchLaneBaselines: vi.fn(async (): Promise<LaneBaselinesResponse> => mockBaselinesResponse),
}));

vi.mock('../api', () => ({
  apiFetch,
  fetchLaneBaselines,
}));

describe('LaneDiagPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiFetch.mockClear();
    fetchLaneBaselines.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows loading state initially', () => {
    render(<LaneDiagPanel />);
    expect(screen.getByText('Loading lane diagnostics…')).toBeInTheDocument();
  });

  it('renders waste taxonomy section after load', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Waste Taxonomy')).toBeInTheDocument();
    expect(screen.getByText('CI Churn')).toBeInTheDocument();
    expect(screen.getByText('Infra')).toBeInTheDocument();
    expect(screen.getByText('Lost Reasoning')).toBeInTheDocument();
    // total waste tasks footer
    expect(screen.getByText(/Total waste tasks: 7/)).toBeInTheDocument();
    expect(screen.getByText('Cap direction')).toBeInTheDocument();
    expect(screen.getByText('Rollup control reviews to converge')).toBeInTheDocument();
  });

  it('renders checkpoint recovery waste section with by_reason breakdown', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Checkpoint Recovery Waste')).toBeInTheDocument();
    expect(screen.getByText('stale_context')).toBeInTheDocument();
    expect(screen.getByText('branch_reset')).toBeInTheDocument();
    expect(screen.getByText(/Total recovery tasks: 6/)).toBeInTheDocument();
  });

  it('renders placeholder when checkpoint_recovery_waste is absent', async () => {
    apiFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ ...mockLaneDiagResponse, checkpoint_recovery_waste: undefined }),
        { status: 200 },
      ),
    );
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getAllByText('No checkpoint-recovery waste recorded.').length).toBeGreaterThan(0);
  });

  it('renders placeholder when checkpoint_recovery_waste total is zero', async () => {
    apiFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...mockLaneDiagResponse,
          checkpoint_recovery_waste: { by_reason: {}, total: 0, exhausted_count: 0 },
        }),
        { status: 200 },
      ),
    );
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getAllByText('No checkpoint-recovery waste recorded.').length).toBeGreaterThan(0);
  });

  it('renders cross-version trend section when projectId and baselines trend are present', async () => {
    render(<LaneDiagPanel projectId="project-1" />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Cross-Version Trend')).toBeInTheDocument();
    expect(screen.getByTestId('cross-version-trend')).toBeInTheDocument();
    expect(screen.getByText('v9 → v10')).toBeInTheDocument();
    expect(screen.getByText('Relay Medians')).toBeInTheDocument();
    expect(screen.getByText('Lane Iterations')).toBeInTheDocument();
    expect(screen.getByText('Conflict p50')).toBeInTheDocument();
    expect(screen.getByText('CI p90')).toBeInTheDocument();
    expect(screen.getByText('Review convergence p50')).toBeInTheDocument();
    expect(screen.getByText('Waste Taxonomy Shares')).toBeInTheDocument();
    expect(screen.getByText('Outer-Loop Convergence')).toBeInTheDocument();
    expect(screen.getByText('Median cycles')).toBeInTheDocument();
    expect(screen.getByText('Cap-exhausted chains')).toBeInTheDocument();
    expect(screen.getByTestId('cap-loss-direction-trend')).toBeInTheDocument();
    expect(screen.getByText('Prior cap loss')).toBeInTheDocument();
    expect(screen.getByText('Latest cap loss')).toBeInTheDocument();
    expect(screen.getByText('d-id-only')).toBeInTheDocument();
    expect(screen.getAllByText('Retarget Convergence')).not.toHaveLength(0);
    expect(screen.getByText('Reviews to converge (median)')).toBeInTheDocument();
    expect(screen.getAllByText('Delta-scope retargets')).not.toHaveLength(0);
    expect(screen.getAllByText('Stale-head discards')).not.toHaveLength(0);
    // classification labels
    expect(screen.getAllByText('↓ improving').length).toBeGreaterThan(0);
  });

  it('does not render cross-version trend when no projectId', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.queryByText('Cross-Version Trend')).not.toBeInTheDocument();
    expect(fetchLaneBaselines).not.toHaveBeenCalled();
  });

  it('does not render cross-version trend when trend is null (single version)', async () => {
    fetchLaneBaselines.mockResolvedValueOnce({ ...mockBaselinesResponse, trend: null });
    render(<LaneDiagPanel projectId="project-1" />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.queryByText('Cross-Version Trend')).not.toBeInTheDocument();
  });

  it('renders relay counts by lane', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Relay Counts by Lane')).toBeInTheDocument();
    expect(screen.getByText('LANE_CI')).toBeInTheDocument();
    expect(screen.getByText('LANE_CONFLICT')).toBeInTheDocument();
    expect(screen.getByText('LANE_REVIEW_CONVERGENCE')).toBeInTheDocument();
  });

  it('renders live retarget convergence counters', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getAllByText('Reviews to converge')).not.toHaveLength(0);
    expect(screen.getAllByText('Delta-scope retargets')).not.toHaveLength(0);
    expect(screen.getAllByText('Full-scope retargets')).not.toHaveLength(0);
    expect(screen.getAllByText('Stale-head discards')).not.toHaveLength(0);
  });

  it('renders missing live retarget counters as unavailable instead of zero', async () => {
    apiFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...mockLaneDiagResponse,
          lane_waste: {
            ...mockLaneDiagResponse.lane_waste,
            retarget_review_scope_counts: undefined,
            stale_head_discard_count: undefined,
          },
        }),
        { status: 200 },
      ),
    );
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Delta-scope retargets').closest('tr')).toHaveTextContent('—');
    expect(screen.getByText('Full-scope retargets').closest('tr')).toHaveTextContent('—');
    expect(screen.getByText('Stale-head discards').closest('tr')).toHaveTextContent('—');
  });

  it('renders a genuine zero live retarget counter as zero', async () => {
    apiFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...mockLaneDiagResponse,
          lane_waste: {
            ...mockLaneDiagResponse.lane_waste,
            retarget_review_scope_counts: { delta: 0, full: 0 },
            stale_head_discard_count: 0,
          },
        }),
        { status: 200 },
      ),
    );
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Delta-scope retargets').closest('tr')).toHaveTextContent('0');
    expect(screen.getByText('Full-scope retargets').closest('tr')).toHaveTextContent('0');
    expect(screen.getByText('Stale-head discards').closest('tr')).toHaveTextContent('0');
  });

  it('surfaces the superseded-rollup median and the directions lost to the cap', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    // The two numbers the lane-waste measurement exists to answer must be
    // readable, not just computed and persisted.
    expect(screen.getByText('Superseded-rollup reviews to converge')).toBeInTheDocument();
    const affectedRow = screen.getByText('Superseded-rollup reviews to converge').closest('tr');
    expect(affectedRow?.textContent).toContain('median of 2 merged of 2 superseded rollup chains, P95 9');
    expect(screen.getByText('Directions lost to the review cap')).toBeInTheDocument();
    expect(screen.getAllByText(/\(2 chains\)/).length).toBeGreaterThan(0);
  });

  it('renders the new convergence trend rows and leaves sample size unclassified', async () => {
    render(<LaneDiagPanel projectId="project-1" />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Directions lost to cap')).toBeInTheDocument();
    expect(
      screen.getByText('Superseded reviews to converge (median)'),
    ).toBeInTheDocument();
    expect(screen.getByText('Superseded reviews to converge (P95)')).toBeInTheDocument();
    // Converging fewer chains is a throughput drop, never an "improving" verdict.
    const sampleRow = screen.getByText('Converged chains (sample size)').closest('tr');
    if (!sampleRow) throw new Error('sample-size trend row was not rendered');
    expect(sampleRow.textContent).not.toContain('improving');
  });

  it('renders evicted and unavailable trend provenance explicitly', async () => {
    fetchLaneBaselines.mockResolvedValueOnce({
      ...mockBaselinesResponse,
      trend: {
        ...mockBaselinesResponse.trend,
        retarget_convergence: {
          ...mockBaselinesResponse.trend.retarget_convergence,
          retarget_review_scope_counts: {
            delta: {
              latest: null,
              prior: 0,
              delta: null,
              classification: null,
              availability: { latest: 'evicted', prior: 'available' },
            },
            full: {
              latest: null,
              prior: null,
              delta: null,
              classification: null,
              availability: { latest: 'unavailable', prior: 'unavailable' },
            },
          },
        },
      },
    });
    render(<LaneDiagPanel projectId="project-1" />);
    await act(async () => { await Promise.resolve(); });

    const deltaTrendRow = screen
      .getAllByText('Delta-scope retargets')
      .find((element) => element.closest('tr')?.textContent?.includes('Evicted'));
    const fullTrendRow = screen
      .getAllByText('Full-scope retargets')
      .find((element) => element.closest('tr')?.textContent?.includes('Unavailable'));
    expect(deltaTrendRow?.closest('tr')).toHaveTextContent('Evicted');
    expect(fullTrendRow?.closest('tr')).toHaveTextContent('Unavailable');
  });

  it('renders None for versions without persisted cap-loss directions', async () => {
    fetchLaneBaselines.mockResolvedValueOnce({
      ...mockBaselinesResponse,
      trend: {
        ...mockBaselinesResponse.trend,
        outer_loop_convergence: {
          ...mockBaselinesResponse.trend.outer_loop_convergence,
          cap_exhausted_directions: { latest: [], prior: [] },
        },
      },
    });
    render(<LaneDiagPanel projectId="project-1" />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByTestId('cap-loss-direction-trend')).toHaveTextContent('None');
    expect(screen.getAllByText('None')).toHaveLength(2);
  });

  it('renders per-PR phase breakdown sorted by total_seconds desc', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('Per-PR Phase Breakdown')).toBeInTheDocument();
    // PR #42 has higher total_seconds and should appear first
    const pr42 = screen.getByText('#42');
    const pr41 = screen.getByText('#41');
    expect(pr42).toBeInTheDocument();
    expect(pr41).toBeInTheDocument();
    // PR #42 appears before PR #41 in document order
    expect(pr42.compareDocumentPosition(pr41) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('passes projectId as query param to apiFetch', async () => {
    render(<LaneDiagPanel projectId="proj-abc" />);
    await act(async () => { await Promise.resolve(); });

    expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining('project_id=proj-abc'),
      undefined,
      2,
      30000,
    );
  });

  it('renders without projectId (no query param)', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(apiFetch).toHaveBeenCalledWith(
      '/api/admin/lane-diag',
      undefined,
      2,
      30000,
    );
  });

  it('renders window label from window_days', async () => {
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText(/7d window/)).toBeInTheDocument();
  });

  it('shows error state when fetch fails', async () => {
    apiFetch.mockResolvedValueOnce(new Response('{}', { status: 500 }));
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText(/Error: HTTP 500/)).toBeInTheDocument();
  });

  it('shows empty relay table when all counts are zero', async () => {
    apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({
      ...mockLaneDiagResponse,
      relay_counts_by_lane: { LANE_CI: 0, LANE_CONFLICT: 0 },
    }), { status: 200 }));
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('No relay data available.')).toBeInTheDocument();
  });

  it('shows version label when version is present', async () => {
    apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({
      ...mockLaneDiagResponse,
      window_days: undefined,
      version: { version_number: 9, phase: 'BUILD', status: 'open', opened_at: '2026-06-01T00:00:00Z', promoted_at: null },
    }), { status: 200 }));
    render(<LaneDiagPanel />);
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText(/v9/)).toBeInTheDocument();
  });
});
