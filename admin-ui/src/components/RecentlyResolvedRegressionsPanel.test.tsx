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
