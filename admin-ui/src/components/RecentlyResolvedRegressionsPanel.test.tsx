import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../api', () => ({ apiFetch: vi.fn(), authHeaders: vi.fn(), API_BASE_URL: '' }));

import { apiFetch } from '../api';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;
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
});
