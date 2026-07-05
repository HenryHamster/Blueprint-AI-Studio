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
      last_tick_at: '2026-07-05T12:00:00Z',
      last_tick: null,
      recent_ticks: [],
      recent_flags: [
        { check_id: 'flag-1', metric: 'memory', expected: '< 0.7', found: '0.81' },
        { check_id: 'flag-2', metric: 'cpu', expected: '< 0.8', found: '0.92' },
      ],
    });

    render(<MetricsIntegrityObservabilityPanel sweepMode="observe" />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(await within(panel).findByText('4 ticks · 2 flags')).toBeInTheDocument();
    expect(within(panel).getByText('11')).toBeInTheDocument();
    expect(within(panel).getByText('Recent Flags')).toBeInTheDocument();
    expect(within(panel).getByText('flag-2')).toBeInTheDocument();
    expect(within(panel).getByText('cpu')).toBeInTheDocument();
    expect(within(panel).getByText('0.92')).toBeInTheDocument();
    expect(within(panel).getByText('Last tick:')).toBeInTheDocument();
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
