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
    LANE_ARBITRATION: 0,
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
    cap_exhausted_chain_count: 0,
    chain_limit: 50,
    chains_truncated: false,
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
      review: {
        p50: { latest: 1.5, prior: 2.0, delta: -0.5, classification: 'stable' },
        p90: { latest: 3.0, prior: 4.0, delta: -1.0, classification: 'improving' },
      },
      fix: {
        p50: { latest: 1.2, prior: 1.5, delta: -0.3, classification: 'stable' },
        p90: { latest: 2.5, prior: 3.0, delta: -0.5, classification: 'stable' },
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
    expect(screen.getByText('Waste Taxonomy Shares')).toBeInTheDocument();
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
    // LANE_ARBITRATION has count 0 so it should not be shown
    expect(screen.queryByText('LANE_ARBITRATION')).not.toBeInTheDocument();
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
