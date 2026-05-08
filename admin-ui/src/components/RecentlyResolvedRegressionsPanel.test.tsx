import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../api', () => ({ apiFetch: vi.fn(), authHeaders: vi.fn(), API_BASE_URL: '', fetchCrossLayerCompliance: vi.fn() }));

import { apiFetch, fetchCrossLayerCompliance } from '../api';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;
const fetchComplianceMock = fetchCrossLayerCompliance as unknown as ReturnType<typeof vi.fn>;
const response = (cases: unknown[]): Response => ({ ok: true, status: 200, json: async () => ({ cases }) } as Response);

beforeEach(() => apiFetchMock.mockReset());

describe('RecentlyResolvedRegressionsPanel', () => {
  it('shows time-to-detect when timestamps are present', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '1', regression_class: 'timeout', fix_pr_number: 42, recovered_at: '2026-05-06T10:15:00', first_seen_at: '2026-05-06T10:10:00', signal_first_crossed_at: '2026-05-06T10:05:00', time_to_recover_sec: 300 }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('timeout');
    expect(screen.getByText('time-to-detect:').parentElement).toHaveTextContent('time-to-detect: 5m');
    expect(screen.getByText('time-to-recover:').parentElement).toHaveTextContent('time-to-recover: 5m');
  });

  it('shows unknown time-to-detect for legacy cases', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '2', regression_class: 'timeout', fix_pr_number: null, recovered_at: null, first_seen_at: '2026-05-06T10:10:00', time_to_recover_sec: null }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('timeout');
    expect(screen.getByText('time-to-detect:').parentElement).toHaveTextContent('time-to-detect: unknown');
  });

  it('renders layer badges and cross-layer chip when layer_summary has multiple layers', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '3',
        regression_class: 'cross-layer-case',
        fix_pr_number: 99,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        layer_summary: {
          cross_layer: true,
          layers_affected: ['AGENT_VM', 'CI_WORKFLOW'],
          window_hours: 48,
          layers: [
            { layer: 'CI_WORKFLOW', count: 4, distinct_fingerprints: 2, last_observed_at: '2026-05-06T10:10:00', sample: 'ci' },
            { layer: 'AGENT_VM', count: 2, distinct_fingerprints: 1, last_observed_at: '2026-05-06T09:50:00', sample: 'vm' },
          ],
        },
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('cross-layer-case');
    expect(screen.getByTestId('layer-badge-CI_WORKFLOW')).toHaveTextContent('CI · 4');
    expect(screen.getByTestId('layer-badge-AGENT_VM')).toHaveTextContent('VM · 2');
    expect(screen.getByTestId('cross-layer-badge')).toBeInTheDocument();
  });

  it('renders single layer badge and no cross-layer chip when only one layer fired', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '4',
        regression_class: 'single-layer-case',
        fix_pr_number: null,
        recovered_at: null,
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: null,
        layer_summary: {
          cross_layer: false,
          layers_affected: ['BASELINE_GATE'],
          window_hours: 48,
          layers: [
            { layer: 'BASELINE_GATE', count: 1, distinct_fingerprints: 1, last_observed_at: '2026-05-06T10:10:00', sample: 'lint baseline' },
          ],
        },
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('single-layer-case');
    expect(screen.getByTestId('layer-badge-BASELINE_GATE')).toHaveTextContent('BASELINE · 1');
    expect(screen.queryByTestId('cross-layer-badge')).toBeNull();
  });

  it('shows time-to-investigate when investigate_spawned_at is present', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '6', regression_class: 'slow-spawn', fix_pr_number: null, recovered_at: null, first_seen_at: '2026-05-06T10:00:00', investigate_spawned_at: '2026-05-06T10:03:00', signal_first_crossed_at: null, time_to_recover_sec: null }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('slow-spawn');
    expect(screen.getByTestId('time-to-investigate')).toHaveTextContent('time-to-investigate: 3m');
  });

  it('shows unknown time-to-investigate for legacy rows without investigate_spawned_at', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '7', regression_class: 'legacy-case', fix_pr_number: null, recovered_at: null, first_seen_at: '2026-05-06T10:00:00', time_to_recover_sec: null }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('legacy-case');
    expect(screen.getByTestId('time-to-investigate')).toHaveTextContent('time-to-investigate: unknown');
  });

  it('renders timeline-breakdown row with all four phases', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '8', regression_class: 'full-timeline', fix_pr_number: 55, recovered_at: '2026-05-06T10:20:00', first_seen_at: '2026-05-06T10:05:00', signal_first_crossed_at: '2026-05-06T10:00:00', investigate_spawned_at: '2026-05-06T10:07:00', merged_at: '2026-05-06T10:15:00', time_to_recover_sec: 300 }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('full-timeline');
    const breakdown = screen.getByTestId('timeline-breakdown');
    expect(breakdown).toHaveTextContent('detect→');
    expect(breakdown).toHaveTextContent('investigate→');
    expect(breakdown).toHaveTextContent('merge→');
    expect(breakdown).toHaveTextContent('recover');
  });

  it('renders no badges when layer_summary is omitted', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '5',
        regression_class: 'legacy-no-summary',
        fix_pr_number: null,
        recovered_at: null,
        first_seen_at: '2026-05-06T10:10:00',
        time_to_recover_sec: null,
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('legacy-no-summary');
    expect(screen.queryByTestId('layers-affected-badges')).toBeNull();
  });

  it('displays cross-layer compliance metric with rollup numbers', async () => {
    apiFetchMock.mockResolvedValueOnce(response([]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 10,
      compliant: 8,
      non_compliant: 2,
      awaiting_backfill_count: 1,
      compliance_rate: 0.8,
      failure_modes: { doc_test_only: 1, most_recent_layer_only: 1 },
      by_class: {},
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('Cross-layer Compliance');
    expect(screen.getByText('8 / 10 (80%)')).toBeInTheDocument();
    expect(screen.getByText('no-canonical: 1')).toBeInTheDocument();
    expect(screen.getByText('recent-layer-only: 1')).toBeInTheDocument();
    expect(screen.getByText('1 awaiting backfill')).toBeInTheDocument();
  });

  it('shows pending classification when awaiting backfill but none classified yet', async () => {
    apiFetchMock.mockResolvedValueOnce(response([]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 0,
      compliant: 0,
      non_compliant: 0,
      awaiting_backfill_count: 3,
      compliance_rate: null,
      failure_modes: { doc_test_only: 0, most_recent_layer_only: 0 },
      by_class: {},
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('Pending classification');
    expect(screen.getByText('(3 awaiting backfill)')).toBeInTheDocument();
  });

  it('shows neutral message when no cross-layer remediations classified', async () => {
    apiFetchMock.mockResolvedValueOnce(response([]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 0,
      compliant: 0,
      non_compliant: 0,
      awaiting_backfill_count: 0,
      compliance_rate: null,
      failure_modes: { doc_test_only: 0, most_recent_layer_only: 0 },
      by_class: {},
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('No cross-layer remediations classified yet');
  });

  it('renders no fragile chip when is_fragile is false or absent', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '6',
        regression_class: 'stable-fix',
        fix_pr_number: 100,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        is_fragile: false,
        hold_days_history: [7.0, 8.5],
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('stable-fix');
    expect(screen.queryByTestId('fragile-chip')).toBeNull();
  });

  it('renders fragile chip with tooltip when is_fragile is true', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '7',
        regression_class: 'fragile-fix',
        fix_pr_number: 101,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        is_fragile: true,
        hold_days_history: [0.5, 1.1, 0.8],
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('fragile-fix');
    const fragileChip = screen.getByTestId('fragile-chip');
    expect(fragileChip).toBeInTheDocument();
    expect(fragileChip).toHaveTextContent('fragile');
    expect(fragileChip).toHaveAttribute('title', '0.5d, 1.1d, 0.8d before re-trip');
  });

  it('renders fragile chip with empty hold_days_history when is_fragile is true but no history', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '8',
        regression_class: 'fragile-edge-case',
        fix_pr_number: 102,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        is_fragile: true,
        hold_days_history: [],
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('fragile-edge-case');
    const fragileChip = screen.getByTestId('fragile-chip');
    expect(fragileChip).toBeInTheDocument();
    expect(fragileChip).toHaveTextContent('fragile');
    expect(fragileChip).not.toHaveAttribute('title');
  });

  it('renders recurrence-by-bucket breakdown when recurrence_by_bucket is present', async () => {
    apiFetchMock.mockResolvedValueOnce(response([]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 10,
      compliant: 8,
      non_compliant: 2,
      awaiting_backfill_count: 0,
      compliance_rate: 0.8,
      failure_modes: { doc_test_only: 1, most_recent_layer_only: 1 },
      by_class: {},
      recurrence_by_bucket: {
        compliant: { merged_count: 8, retripped_count: 1, recurrence_rate: 0.125, median_hold_days: 7.5 },
        most_recent_layer_only: { merged_count: 1, retripped_count: 1, recurrence_rate: 1.0, median_hold_days: 2.0 },
      },
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByTestId('recurrence-by-bucket');
    expect(screen.getByText('Recurrence by compliance bucket')).toBeInTheDocument();
    expect(screen.getByText('compliant')).toBeInTheDocument();
    expect(screen.getByText('1/8')).toBeInTheDocument();
    expect(screen.getByText('13%')).toBeInTheDocument();
    expect(screen.getByText(/7\.5d median/)).toBeInTheDocument();
    expect(screen.getByText('recent-only')).toBeInTheDocument();
    expect(screen.getByText('1/1')).toBeInTheDocument();
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(screen.getByText(/2d median/)).toBeInTheDocument();
  });

  it('does not render recurrence breakdown when recurrence_by_bucket is absent', async () => {
    apiFetchMock.mockResolvedValueOnce(response([]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 10,
      compliant: 8,
      non_compliant: 2,
      awaiting_backfill_count: 0,
      compliance_rate: 0.8,
      failure_modes: { doc_test_only: 1, most_recent_layer_only: 1 },
      by_class: {},
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('Cross-layer Compliance');
    expect(screen.queryByTestId('recurrence-by-bucket')).toBeNull();
  });

  it('renders marginal chip when last_remediation_result is recovered_marginal', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '10',
        regression_class: 'marginal-fix',
        fix_pr_number: 104,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        last_remediation_result: 'recovered_marginal',
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('marginal-fix');
    const chip = screen.getByTestId('marginal-chip');
    expect(chip).toHaveTextContent('marginal');
    expect(chip).toHaveAttribute('title', 'Marginal improvement — signal may re-trip on noise');
  });

  it('does not render marginal chip when last_remediation_result is recovered_unattended', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '11',
        regression_class: 'clean-fix',
        fix_pr_number: 105,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        last_remediation_result: 'recovered_unattended',
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('clean-fix');
    expect(screen.queryByTestId('marginal-chip')).toBeNull();
  });

  it('shows marginal count in compliance section when marginal cases are present', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      { id: '12', regression_class: 'marginal-a', fix_pr_number: null, recovered_at: null, first_seen_at: null, time_to_recover_sec: null, last_remediation_result: 'recovered_marginal' },
      { id: '13', regression_class: 'marginal-b', fix_pr_number: null, recovered_at: null, first_seen_at: null, time_to_recover_sec: null, last_remediation_result: 'recovered_marginal' },
    ]));
    fetchComplianceMock.mockResolvedValueOnce({
      total_classified: 5,
      compliant: 4,
      non_compliant: 1,
      awaiting_backfill_count: 0,
      compliance_rate: 0.8,
      failure_modes: { doc_test_only: 0, most_recent_layer_only: 1 },
      by_class: {},
    });
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('Cross-layer Compliance');
    const countEl = screen.getByTestId('marginal-count');
    expect(countEl).toHaveTextContent('2 marginal recovery (signal may re-trip)');
  });

  it('renders both recurrence and fragile chips when applicable', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        id: '9',
        regression_class: 'multi-chip-case',
        fix_pr_number: 103,
        recovered_at: '2026-05-06T10:15:00',
        first_seen_at: '2026-05-06T10:10:00',
        signal_first_crossed_at: '2026-05-06T10:05:00',
        time_to_recover_sec: 300,
        recurrence_count: 2,
        is_fragile: true,
        hold_days_history: [0.5],
      },
    ]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('multi-chip-case');
    expect(screen.getByText(/recurred 2x/)).toBeInTheDocument();
    expect(screen.getByTestId('fragile-chip')).toBeInTheDocument();
  });
});
