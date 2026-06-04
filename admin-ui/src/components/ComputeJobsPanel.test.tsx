import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import type { ComputeJob } from '../api';
import { ComputeJobsPanel } from './ComputeJobsPanel';
import {
  isComputeJobActive,
  computeJobTerminalLabel,
  fmtCountdown,
} from './ComputeJobsPanel.utils';

const fetchComputeJobs = vi.fn();
const killComputeJob = vi.fn();

vi.mock('../api', () => ({
  fetchComputeJobs: (...args: unknown[]) => fetchComputeJobs(...args),
  killComputeJob: (...args: unknown[]) => killComputeJob(...args),
}));

function makeJob(overrides: Partial<ComputeJob> = {}): ComputeJob {
  return {
    id: 'job-1',
    project_id: 'proj-1',
    task_id: 'task-1',
    backend: 'gcp_compute_engine',
    external_compute_id: 'compute-job-vm-1',
    machine_type: 'e2-standard-2',
    status: 'RUNNING',
    terminal_reason: null,
    outcome: null,
    spend_cap_usd: 4.0,
    wall_clock_deadline: new Date(Date.now() + 2 * 3600 * 1000).toISOString(),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    completed_at: null,
    last_polled_at: new Date().toISOString(),
    accrued_cost_usd: 1.0,
    final_cost_usd: null,
    task_title: 'compute_experiment: sweep',
    task_type: 'compute_experiment',
    ...overrides,
  };
}

describe('ComputeJobsPanel helpers', () => {
  test('isComputeJobActive covers pre-terminal statuses only', () => {
    for (const s of ['QUEUED', 'STARTING', 'RUNNING', 'POLLING'] as const) {
      expect(isComputeJobActive(makeJob({ status: s }))).toBe(true);
    }
    for (const s of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'] as const) {
      expect(isComputeJobActive(makeJob({ status: s }))).toBe(false);
    }
  });

  test('computeJobTerminalLabel distinguishes the three kill paths', () => {
    expect(computeJobTerminalLabel(makeJob({ outcome: 'SPEND_CAP_EXCEEDED' }))).toBe('Killed: budget cap');
    expect(computeJobTerminalLabel(makeJob({ outcome: 'TIMED_OUT' }))).toBe('Killed: deadline');
    expect(
      computeJobTerminalLabel(makeJob({ outcome: 'CANCELLED', terminal_reason: 'killed by operator (a@b.com)' })),
    ).toBe('Killed by operator');
    expect(
      computeJobTerminalLabel(makeJob({ outcome: 'CANCELLED', terminal_reason: 'owning task is terminal' })),
    ).toBe('Cancelled');
    expect(computeJobTerminalLabel(makeJob({ outcome: 'SUCCESS' }))).toBe('Succeeded');
  });

  test('fmtCountdown renders compact remaining time and expiry', () => {
    const now = Date.now();
    expect(fmtCountdown(new Date(now + 90_000).toISOString(), now)).toBe('1m 30s');
    expect(fmtCountdown(new Date(now - 1000).toISOString(), now)).toBe('expired');
    expect(fmtCountdown(null, now)).toBe('—');
  });
});

describe('ComputeJobsPanel render + kill', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    killComputeJob.mockReset();
    vi.restoreAllMocks();
  });

  test('renders budget bar and task title for an active job', async () => {
    fetchComputeJobs.mockResolvedValue([makeJob({ accrued_cost_usd: 2.0, spend_cap_usd: 4.0 })]);
    const { findByText, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    // Budget readout reflects spent / cap.
    expect(container.textContent).toContain('$2.00 / $4.00');
    // Active job exposes a Kill control.
    expect(await findByText('Kill')).toBeTruthy();
  });

  test('kill button calls killComputeJob and shows killed reason', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const active = makeJob();
    fetchComputeJobs.mockResolvedValue([active]);
    killComputeJob.mockResolvedValue(
      makeJob({ status: 'CANCELLED', outcome: 'CANCELLED', terminal_reason: 'killed by operator (a@b.com)', final_cost_usd: 1.0 }),
    );
    const { findByText, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const killBtn = await findByText('Kill');
    await act(async () => { killBtn.click(); });
    await waitFor(() => expect(killComputeJob).toHaveBeenCalledWith('job-1'));
    await findByText('Killed by operator');
    expect(queryByText('Kill')).toBeNull();
  });

  test('renders nothing when there are no jobs', async () => {
    fetchComputeJobs.mockResolvedValue([]);
    const { container } = render(<ComputeJobsPanel projectId="proj-1" />);
    await waitFor(() => expect(fetchComputeJobs).toHaveBeenCalled());
    await waitFor(() => expect(container.textContent).not.toContain('Compute jobs'));
  });
});
