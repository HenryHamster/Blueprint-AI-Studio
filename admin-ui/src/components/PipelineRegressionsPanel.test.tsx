import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../api', () => ({ apiFetch: vi.fn(), authHeaders: vi.fn(), API_BASE_URL: '' }));

import { apiFetch } from '../api';
import { ManualInterventionPanel, AutoRecoveredPanel } from './PipelineRegressionsPanel';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;
const response = (cases: unknown[]): Response =>
  ({ ok: true, status: 200, json: async () => ({ cases }) }) as Response;

beforeEach(() => apiFetchMock.mockReset());

const baseCase = {
  id: 'mi-1',
  regression_class: 'sr_catch_rate:correctness',
  signal_name: 'sr catch rate correctness',
  status: 'NEEDS_MANUAL_INTERVENTION',
  attempts: 3,
  last_remediation_pr_number: 4001,
  last_failure_class: null,
  signal_snapshot_at_spawn: { hit_rate: 0.42 },
  signal_snapshot_at_merge: { hit_rate: 0.55 },
  fingerprint_to_fix_log: [],
  last_seen_at: '2026-05-07T11:00:00',
  needs_manual_intervention_at: '2026-05-07T11:05:00',
  signal_first_seen_at: '2026-05-07T10:30:00',
  time_to_detect_sec: 600,
  escalation_reason: 'attempts_exhausted',
};

describe('ManualInterventionPanel — layer badges', () => {
  it('renders layer badges and cross-layer chip when case has cross-layer summary', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        layer_summary: {
          cross_layer: true,
          layers_affected: ['AGENT_VM', 'CI_WORKFLOW'],
          window_hours: 48,
          layers: [
            { layer: 'CI_WORKFLOW', count: 5, distinct_fingerprints: 2, last_observed_at: '2026-05-07T10:30:00', sample: 'ci' },
            { layer: 'AGENT_VM', count: 2, distinct_fingerprints: 1, last_observed_at: '2026-05-07T10:00:00', sample: 'vm' },
          ],
        },
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    expect(screen.getByTestId('layer-badge-CI_WORKFLOW')).toHaveTextContent('CI · 5');
    expect(screen.getByTestId('layer-badge-AGENT_VM')).toHaveTextContent('VM · 2');
    expect(screen.getByTestId('cross-layer-badge')).toBeInTheDocument();
  });

  it('renders single layer badge with no cross-layer chip', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        layer_summary: {
          cross_layer: false,
          layers_affected: ['REVIEWER_PASS'],
          window_hours: 48,
          layers: [
            { layer: 'REVIEWER_PASS', count: 2, distinct_fingerprints: 1, last_observed_at: '2026-05-07T10:30:00', sample: '' },
          ],
        },
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    expect(screen.getByTestId('layer-badge-REVIEWER_PASS')).toHaveTextContent('REVIEWER · 2');
    expect(screen.queryByTestId('cross-layer-badge')).toBeNull();
  });

  it('renders no badges when layer_summary is omitted', async () => {
    apiFetchMock.mockResolvedValueOnce(response([baseCase]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    expect(screen.queryByTestId('layers-affected-badges')).toBeNull();
  });
});

describe('AutoRecoveredPanel — layer badges', () => {
  it('renders layer badges in auto-recovered cases', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        id: 'ar-1',
        regression_class: 'auto-recovered-case',
        escalation_reason: 'recovered_unattended',
        layer_summary: {
          cross_layer: true,
          layers_affected: ['AGENT_VERB', 'CI_WORKFLOW'],
          window_hours: 48,
          layers: [
            { layer: 'AGENT_VERB', count: 1, distinct_fingerprints: 1, last_observed_at: '2026-05-07T10:00:00', sample: '' },
            { layer: 'CI_WORKFLOW', count: 1, distinct_fingerprints: 1, last_observed_at: '2026-05-07T09:30:00', sample: '' },
          ],
        },
      },
    ]));
    render(<AutoRecoveredPanel projectId="proj-1" />);
    await screen.findByText('auto-recovered-case');
    expect(screen.getByTestId('layer-badge-AGENT_VERB')).toHaveTextContent('VERB · 1');
    expect(screen.getByTestId('layer-badge-CI_WORKFLOW')).toHaveTextContent('CI · 1');
    expect(screen.getByTestId('cross-layer-badge')).toBeInTheDocument();
  });
});
