import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const { fetchMetricsIntegrityObservabilityMock } = vi.hoisted(() => ({
  fetchMetricsIntegrityObservabilityMock: vi.fn(),
}));

vi.mock('../api', () => ({
  fetchMetricsIntegrityObservability: (...args: unknown[]) => fetchMetricsIntegrityObservabilityMock(...args),
}));

import { MetricsIntegrityObservabilityPanel } from './MetricsIntegrityObservabilityPanel';
import { __pollerInternals } from '../hooks/usePoller';

describe('MetricsIntegrityObservabilityPanel', () => {
  beforeEach(() => {
    fetchMetricsIntegrityObservabilityMock.mockReset();
    __pollerInternals.reset();
  });

  it('renders the poller payload and recent flags table', async () => {
    fetchMetricsIntegrityObservabilityMock.mockResolvedValueOnce({
      ticks: 4,
      checks_run: 11,
      flags_total: 2,
      errors_total: 1,
      enrolled_total: 3,
      auto_resolved_total: 5,
      auto_resolved_backing_path_total: 3,
      auto_resolved_named_test_present_total: 2,
      last_tick_at: '2026-07-05T12:00:00Z',
      last_tick: null,
      recent_ticks: [],
      recent_flags: [
        {
          check: 'capability_proof_gate_gap',
          status: 'flagged',
          self_reported: 10,
          ground_truth: 7,
          delta: -3,
          severity: 'warn',
          subject: 'proj-abc',
          detail: '3 capabilities self-reported working but have no proof',
          auto_resolved: 2,
          auto_resolved_backing_path: 1,
          auto_resolved_named_test_present: 1,
          at: '2026-07-05T12:00:00Z',
        },
        {
          check: 'metrics_drift',
          status: 'flagged',
          self_reported: 5,
          ground_truth: 8,
          delta: 3,
          severity: 'error',
          subject: 'global',
          detail: 'task completion rate drifted above threshold',
          auto_resolved: null,
          auto_resolved_backing_path: null,
          auto_resolved_named_test_present: null,
          at: '2026-07-05T12:00:00Z',
        },
      ],
    });

    render(<MetricsIntegrityObservabilityPanel sweepMode="observe" />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(await within(panel).findByText('4 ticks · 2 flags')).toBeInTheDocument();
    expect(within(panel).getByText('11')).toBeInTheDocument();
    expect(within(panel).getByText('Recent Flags')).toBeInTheDocument();
    // Real backend field: check name
    expect(within(panel).getByText('metrics_drift')).toBeInTheDocument();
    // Real backend field: subject
    expect(within(panel).getByText('global')).toBeInTheDocument();
    // Self-heal stat tile renders auto_resolved_total
    expect(within(panel).getByText('Auto-Resolved').closest('.bg-workbench-base')).toHaveTextContent('5');
    expect(within(panel).getByText('Last tick:')).toBeInTheDocument();
    // Column headers match the real backend contract
    expect(within(panel).getByText('Self-Reported')).toBeInTheDocument();
    expect(within(panel).getByText('Ground Truth')).toBeInTheDocument();
    expect(within(panel).getByText('Delta')).toBeInTheDocument();
  });

  it('shows the no-ticks empty state when the poller returns zero activity', async () => {
    fetchMetricsIntegrityObservabilityMock.mockResolvedValueOnce({
      ticks: 0,
      checks_run: 0,
      flags_total: 0,
      errors_total: 0,
      enrolled_total: 0,
      last_tick_at: null,
      last_tick: null,
      recent_ticks: [],
      recent_flags: [],
    });

    render(<MetricsIntegrityObservabilityPanel />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(await within(panel).findByText('No metrics integrity sweep ticks recorded yet.')).toBeInTheDocument();
    expect(within(panel).queryByText('Recent Flags')).not.toBeInTheDocument();
    expect(within(panel).queryByText('No drift flags recorded in recent ticks.')).not.toBeInTheDocument();
  });

  it('invokes the sweep-mode callback when a mode button is clicked', async () => {
    fetchMetricsIntegrityObservabilityMock.mockResolvedValueOnce({
      ticks: 0,
      checks_run: 0,
      flags_total: 0,
      errors_total: 0,
      enrolled_total: 0,
      last_tick_at: null,
      last_tick: null,
      recent_ticks: [],
      recent_flags: [],
    });

    const onSweepModeChange = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<MetricsIntegrityObservabilityPanel sweepMode="off" onSweepModeChange={onSweepModeChange} />);

    await user.click(screen.getByRole('button', { name: 'enforce' }));
    expect(onSweepModeChange).toHaveBeenCalledWith('enforce');
  });
});
