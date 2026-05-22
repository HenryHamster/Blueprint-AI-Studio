import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

vi.mock('../api', () => ({ apiFetch: vi.fn(), authHeaders: vi.fn(), API_BASE_URL: '' }));

import { apiFetch } from '../api';
import { ManualInterventionPanel, AutoRecoveredPanel, EscalationOutcomePanel } from './PipelineRegressionsPanel';

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

describe('ManualInterventionPanel — phase chips', () => {
  it('renders investigate→ chip with formatted duration when investigate_spawned_at is set', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{
      ...baseCase,
      signal_first_seen_at: '2026-05-07T10:00:00',
      signal_first_crossed_at: '2026-05-07T09:50:00',
      investigate_spawned_at: '2026-05-07T10:10:00',
      time_to_investigate_sec: 600,
    }]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    const timeline = screen.getByTestId('active-phase-timeline');
    expect(timeline).toBeInTheDocument();
    expect(timeline).toHaveTextContent('investigate→');
    expect(timeline).not.toHaveTextContent('pending');
  });

  it('renders pending when investigate_spawned_at is null', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{
      ...baseCase,
      signal_first_seen_at: '2026-05-07T10:00:00',
      signal_first_crossed_at: '2026-05-07T09:50:00',
      investigate_spawned_at: null,
      time_to_investigate_sec: null,
    }]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    const timeline = screen.getByTestId('active-phase-timeline');
    expect(timeline).toHaveTextContent('pending');
  });

  it('renders amber on pending chip when case is older than 2h', async () => {
    const twoHoursAgo = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
    apiFetchMock.mockResolvedValueOnce(response([{
      ...baseCase,
      signal_first_seen_at: twoHoursAgo,
      signal_first_crossed_at: null,
      investigate_spawned_at: null,
      time_to_investigate_sec: null,
    }]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    const timeline = screen.getByTestId('active-phase-timeline');
    const pendingSpan = Array.from(timeline.querySelectorAll('span')).find(
      (el) => el.textContent === 'pending'
    );
    expect(pendingSpan).toBeDefined();
    expect(pendingSpan?.className).toContain('amber');
  });
});

describe('ManualInterventionPanel — layer mismatch badge', () => {
  it('shows no badge when layer_mismatch is absent', async () => {
    apiFetchMock.mockResolvedValueOnce(response([baseCase]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    expect(screen.queryByTestId('layer-mismatch-badge')).toBeNull();
  });

  it('shows no badge when layer_mismatch is false', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        layer_summary: {
          cross_layer: false,
          layers_affected: ['CI_WORKFLOW'],
          window_hours: 48,
          layers: [],
          layer_mismatch: false,
          prior_fix_layers: ['CI_WORKFLOW'],
          current_symptom_layers: ['CI_WORKFLOW'],
        },
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    expect(screen.queryByTestId('layer-mismatch-badge')).toBeNull();
  });

  it('shows badge with disjoint-layer tooltip when layer_mismatch is true', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        layer_summary: {
          cross_layer: false,
          layers_affected: ['AGENT_VM'],
          window_hours: 48,
          layers: [],
          layer_mismatch: true,
          prior_fix_layers: ['CI_WORKFLOW'],
          current_symptom_layers: ['AGENT_VM'],
        },
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('sr_catch_rate:correctness');
    const badge = screen.getByTestId('layer-mismatch-badge');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent('layer mismatch');
    expect(badge).toHaveAttribute('title', 'prior fixes targeted CI; current symptoms on VM');
  });
});

describe('ManualInterventionPanel — Acknowledge button', () => {
  it('POSTs to /acknowledge with project_id query param', async () => {
    apiFetchMock.mockResolvedValueOnce(response([baseCase]));
    apiFetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ id: baseCase.id, operator_acknowledged_at: '2026-05-08T12:00:00+00:00' }),
    } as Response);

    render(<ManualInterventionPanel projectId="proj-abc" />);
    const button = await screen.findByRole('button', { name: /Acknowledge/ });
    fireEvent.click(button);

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(2));
    const ackCall = apiFetchMock.mock.calls[1];
    expect(ackCall[0]).toBe(
      `/api/admin/regression-cases/${encodeURIComponent(baseCase.id)}/acknowledge?project_id=proj-abc`
    );
    expect(ackCall[1]).toEqual({ method: 'POST' });
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

const escalationResponse = (byReason: Record<string, unknown>): Response =>
  ({ ok: true, status: 200, json: async () => ({ by_reason: byReason, lookback_days: 30 }) }) as Response;

describe('ManualInterventionPanel — viewport failure screenshot disclosure', () => {
  const viewportCase = {
    ...baseCase,
    regression_class: 'viewport_regression:MessagesPanel@375',
  };

  it('shows failure screenshot when failure_screenshot_url is set', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{
      ...viewportCase,
      failure_screenshot_url: '/uploads/viewport-failures/MessagesPanel-375px-abc.png',
    }]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('viewport regression');
    const toggle = screen.getByRole('button', { name: /failure screenshot/ });
    fireEvent.click(toggle);
    expect(screen.queryByAltText('MessagesPanel baseline at 375px')).toBeNull();
    expect(screen.getByAltText('MessagesPanel failure at 375px')).toBeInTheDocument();
    expect(screen.getByText('Latest failure')).toBeInTheDocument();
  });

  it('hides the disclosure button when failure_screenshot_url is null', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ ...viewportCase, failure_screenshot_url: null }]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await screen.findByText('viewport regression');
    expect(screen.queryByRole('button', { name: /failure screenshot/ })).toBeNull();
  });
});

describe('EscalationOutcomePanel — compliance_followup bucket', () => {
  it('renders both cross-layer baseline and compliance_followup rows as separate entries', async () => {
    apiFetchMock.mockResolvedValueOnce(escalationResponse({
      escalated_cross_layer: { merged_count: 3, retripped_within_30d_count: 1, durable_resolution_rate: 0.67, median_hold_days: 12.5, in_flight_excluded_count: 0, prevention_rate_30d: 0.70 },
      compliance_followup: { merged_count: 2, retripped_within_30d_count: 0, durable_resolution_rate: 1.0, median_hold_days: 18.3, in_flight_excluded_count: 0, prevention_rate_30d: 0.85 },
    }));
    render(<EscalationOutcomePanel projectId="proj-1" />);
    await screen.findAllByText('Cross-layer');
    // Both mobile card and desktop table render the labels — scope to the table for the unique assertion
    const table = screen.getByRole('table');
    expect(within(table).getByText('↳ Compliance re-fix')).toBeInTheDocument();
    // baseline row shows merged_count 3, followup row shows 2 — they are independent
    const cells = screen.getAllByRole('cell');
    const mergedCounts = cells.filter(c => c.textContent === '3' || c.textContent === '2');
    expect(mergedCounts.length).toBeGreaterThanOrEqual(2);
  });

  it('shows compliance_followup row with higher durable_resolution_rate than baseline', async () => {
    apiFetchMock.mockResolvedValueOnce(escalationResponse({
      escalated_cross_layer: { merged_count: 4, retripped_within_30d_count: 2, durable_resolution_rate: 0.5, median_hold_days: 8.0, in_flight_excluded_count: 0, prevention_rate_30d: 0.60 },
      compliance_followup: { merged_count: 2, retripped_within_30d_count: 0, durable_resolution_rate: 1.0, median_hold_days: 20.0, in_flight_excluded_count: 0, prevention_rate_30d: 0.90 },
    }));
    render(<EscalationOutcomePanel projectId="proj-1" />);
    await screen.findAllByText('Cross-layer');
    // baseline durable_resolution_rate = 50%; followup = 100% — scope to table to avoid mobile card duplicates
    const table = screen.getByRole('table');
    expect(within(table).getByText('50%')).toBeInTheDocument();
    expect(within(table).getByText('100%')).toBeInTheDocument();
  });
});

describe('ManualInterventionPanel — viewport regression class parsing', () => {
  it('parses component name correctly for new-format regression (Component:assertion_type@Width)', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        id: 'vp-1',
        regression_class: 'viewport_regression:MessagesPanel:overflow@375',
        signal_name: 'viewport overflow',
        failure_screenshot_url: '/uploads/viewport-failures/MessagesPanel-375px-abc.png',
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('viewport overflow')).toBeInTheDocument());
    const button = screen.getAllByRole('button', { name: /failure screenshot/ })[0];
    fireEvent.click(button);
    // Alt text uses component name without the assertion_type segment
    expect(screen.getByAltText('MessagesPanel failure at 375px')).toBeInTheDocument();
  });

  it('parses component name correctly for legacy-format regression (Component@Width)', async () => {
    apiFetchMock.mockResolvedValueOnce(response([
      {
        ...baseCase,
        id: 'vp-2',
        regression_class: 'viewport_regression:TaskCard@768',
        signal_name: 'viewport legacy',
        failure_screenshot_url: '/uploads/viewport-failures/TaskCard-768px-xyz.png',
      },
    ]));
    render(<ManualInterventionPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('viewport legacy')).toBeInTheDocument());
    const button = screen.getAllByRole('button', { name: /failure screenshot/ })[0];
    fireEvent.click(button);
    expect(screen.getByAltText('TaskCard failure at 768px')).toBeInTheDocument();
  });
});
