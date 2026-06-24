import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import type { ComputeJob } from '../api';
import { ComputeJobsPanel } from './ComputeJobsPanel';
import {
  isComputeJobActive,
  computeJobTerminalLabel,
  computeJobRpcDiagnostic,
  computeJobRuntimeSec,
  computeJobMargin,
  fmtCountdown,
  fmtDuration,
} from './ComputeJobsPanel.utils';
import type { ComputeRpcDiagnostic } from '../api';

const fetchComputeJobs = vi.fn();
const killComputeJob = vi.fn();
const analyzeComputeJob = vi.fn();
const fetchKnowledgeDocument = vi.fn();
const resolveArtifactDownloadUrl = vi.fn();

vi.mock('../api', () => ({
  fetchComputeJobs: (...args: unknown[]) => fetchComputeJobs(...args),
  killComputeJob: (...args: unknown[]) => killComputeJob(...args),
  analyzeComputeJob: (...args: unknown[]) => analyzeComputeJob(...args),
  fetchKnowledgeDocument: (...args: unknown[]) => fetchKnowledgeDocument(...args),
  resolveArtifactDownloadUrl: (...args: unknown[]) => resolveArtifactDownloadUrl(...args),
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
    result_reference: null,
    artifact_id: null,
    accelerator_type: null,
    accelerator_count: null,
    task_title: 'compute_experiment: sweep',
    task_type: 'compute_experiment',
    ...overrides,
  };
}

function makeDiagnostic(overrides: Partial<ComputeRpcDiagnostic> = {}): ComputeRpcDiagnostic {
  return {
    operation: 'poll',
    error_type: 'TimeoutError',
    error_message: 'backend poll timed out after 30s',
    started_at: new Date().toISOString(),
    failed_at: new Date().toISOString(),
    duration_ms: 30000,
    attempt_count: 2,
    job_status: 'RUNNING',
    next_retry_at: new Date(Date.now() + 90_000).toISOString(),
    retry_after_seconds: 90,
    ...overrides,
  };
}

describe('ComputeJobsPanel helpers', () => {
  test('computeJobRpcDiagnostic reads field, falls back to polling_metadata, else null', () => {
    const diag = makeDiagnostic();
    expect(computeJobRpcDiagnostic(makeJob({ compute_rpc_diagnostic: diag }))).toEqual(diag);
    expect(
      computeJobRpcDiagnostic(makeJob({ polling_metadata: { compute_rpc_diagnostic: diag } })),
    ).toEqual(diag);
    expect(computeJobRpcDiagnostic(makeJob())).toBeNull();
    expect(computeJobRpcDiagnostic(makeJob({ compute_rpc_diagnostic: null }))).toBeNull();
  });

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

  test('fmtDuration formats seconds, minutes, and hours', () => {
    expect(fmtDuration(45)).toBe('45s');
    expect(fmtDuration(90)).toBe('1m 30s');
    expect(fmtDuration(3600)).toBe('1h');
    expect(fmtDuration(3660)).toBe('1h 1m');
  });

  test('computeJobRuntimeSec returns elapsed seconds between started_at and completed_at', () => {
    const start = new Date('2026-06-23T10:00:00Z');
    const end = new Date('2026-06-23T10:05:23Z');
    expect(computeJobRuntimeSec(makeJob({ started_at: start.toISOString(), completed_at: end.toISOString() }))).toBe(323);
    expect(computeJobRuntimeSec(makeJob({ started_at: null, completed_at: end.toISOString() }))).toBeNull();
    expect(computeJobRuntimeSec(makeJob({ started_at: start.toISOString(), completed_at: null }))).toBeNull();
  });

  test('computeJobMargin calculates distance from threshold for >= operators', () => {
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
    }))).toBeCloseTo(0.05);
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.85, met: false, measurable: true },
    }))).toBeCloseTo(-0.05);
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'accuracy', operator: '>', threshold: 0.9, value: 0.92, met: true, measurable: true },
    }))).toBeCloseTo(0.02);
  });

  test('computeJobMargin calculates distance from threshold for <= operators', () => {
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 95, met: true, measurable: true },
    }))).toBeCloseTo(5);
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 105, met: false, measurable: true },
    }))).toBeCloseTo(-5);
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'latency_ms', operator: '<', threshold: 100, value: 98, met: true, measurable: true },
    }))).toBeCloseTo(2);
  });

  test('computeJobMargin returns null when value or threshold is missing', () => {
    expect(computeJobMargin(makeJob({
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: null, met: false, measurable: false },
    }))).toBeNull();
    expect(computeJobMargin(makeJob({ criterion_verdict: null }))).toBeNull();
    expect(computeJobMargin(makeJob())).toBeNull();
  });
});

describe('ComputeJobsPanel render + kill', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    killComputeJob.mockReset();
    analyzeComputeJob.mockReset();
    fetchKnowledgeDocument.mockReset();
    resolveArtifactDownloadUrl.mockReset();
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

  test('shows a live-proof indicator when polling_metadata carries live_release_evidence', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        polling_metadata: {
          live_release_evidence: {
            release_version_number: 8,
            promoted_sha: 'abcdef1234567890deadbeef',
            aggregate_serving_status: 'serving_confirmed',
          },
        },
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const chip = await findByTestId('release-evidence-chip');
    expect(chip.textContent).toContain('Live proof of v8');
    // Short SHA only — first 8 chars, not the full hash.
    expect(chip.textContent).toContain('abcdef12');
    expect(chip.textContent).not.toContain('abcdef1234567890deadbeef');
  });

  test('shows no live-proof indicator when live_release_evidence is absent', async () => {
    fetchComputeJobs.mockResolvedValue([makeJob()]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('release-evidence-chip')).toBeNull();
  });

  test('calls onNavigateToTask when task title is clicked', async () => {
    const onNavigate = vi.fn();
    fetchComputeJobs.mockResolvedValue([makeJob({ accrued_cost_usd: 2.0, spend_cap_usd: 4.0 })]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" onNavigateToTask={onNavigate} />);
    const taskTitle = await findByText('compute_experiment: sweep');
    await act(async () => { taskTitle.click(); });
    expect(onNavigate).toHaveBeenCalledWith('task-1');
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

  test('run analysis shows upgraded feedback and refreshes the job list', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockResolvedValue({ status: 'upgraded', analysis_mode: 'llm' });
    const { findByRole, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('Analysis complete');
    await waitFor(() => expect(analyzeComputeJob).toHaveBeenCalledWith('job-1'));
    await waitFor(() => expect(fetchComputeJobs).toHaveBeenCalledTimes(2));
  });

  test('run analysis surfaces unavailable cause labels', async () => {
    const terminal = makeJob({ status: 'FAILED', outcome: 'FAILURE', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockResolvedValue({ status: 'analysis_unavailable', reason: 'analysis-disabled' });
    const { findByRole, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('Analysis unavailable: Analysis disabled');
  });

  test('run analysis surfaces backend errors inline', async () => {
    const terminal = makeJob({ status: 'FAILED', outcome: 'FAILURE', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockRejectedValue(new Error('analysis endpoint failed: upstream 503'));
    const { findByRole, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('analysis endpoint failed: upstream 503');
  });

  test('renders a safe terminal result link', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        accrued_cost_usd: 1.0,
        final_cost_usd: 1.0,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
      }),
    ]);
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    const link = await findByRole('link', { name: /https:\/\/storage\.example\.com\/jobs\/job-1\/result\.json/i });
    expect(link).toHaveAttribute('href', 'https://storage.example.com/jobs/job-1/result.json');
  });

  test('resolves a gs:// terminal result link through the admin API before opening', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue({ opener: null, location: { replace: vi.fn() }, close: vi.fn() } as never);
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed.example.com/jobs/job-1/result.json');
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        result_reference: 'gs://blueprint-agent-artifacts/tasks/task-1/result.json',
      }),
    ]);
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: /gs:\/\/blueprint-agent-artifacts\/tasks\/task-1\/result\.json/i });
    await act(async () => { button.click(); });
    await waitFor(() => expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith(
      'proj-1',
      'gs://blueprint-agent-artifacts/tasks/task-1/result.json',
    ));
    expect(openSpy).toHaveBeenCalled();
  });

  test('surfaces COMPUTE_RESULT artifact with a resolvable reference link', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        artifact_id: 'artifact-abc',
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
      }),
    ]);
    const { findByText, getByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Primary artifact affordance is an openable link.
    const artifactLink = await findByText('COMPUTE_RESULT artifact');
    expect(artifactLink.closest('a')).toHaveAttribute(
      'href',
      'https://storage.example.com/jobs/job-1/result.json',
    );
    // Raw storage URI demoted to secondary technical detail.
    expect(getByText('Result reference')).toBeInTheDocument();
  });

  test('surfaces COMPUTE_RESULT artifact even when result_reference is null (non-openable)', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        artifact_id: 'artifact-abc',
        result_reference: null,
      }),
    ]);
    const { findByText, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const artifact = await findByText('COMPUTE_RESULT artifact');
    // Present but not an openable link (no reference to resolve).
    expect(artifact.closest('a')).toBeNull();
    expect(artifact.closest('button')).toBeNull();
    expect(queryByText('Result reference')).toBeNull();
  });

  test('shows no artifact affordance for jobs without artifact_id', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
    ]);
    const { findByText, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText(/compute_experiment: sweep/i);
    expect(queryByText('COMPUTE_RESULT artifact')).toBeNull();
  });

  test('GPU job shows accelerator label alongside machine type', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        accelerator_type: 'T4',
        accelerator_count: 1,
      }),
    ]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    const label = await findByText((text) => text.includes('T4 x1'));
    expect(label).toBeTruthy();
  });

  test('terminal GPU job with result_metrics surfaces metric key-value pairs', async () => {
    const gpuJob = makeJob({
      id: 'job-gpu',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      accelerator_type: 'T4',
      accelerator_count: 1,
      polling_metadata: { result_metrics: { gpu_count: 1, driver_version: '525.0' } },
    });
    fetchComputeJobs.mockResolvedValue([gpuJob]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(await findByText((t) => t.includes('gpu_count:'))).toBeTruthy();
    expect(await findByText((t) => t.includes('driver_version:'))).toBeTruthy();
  });

  test('terminal CPU (no-accelerator) job with result_metrics shows those metrics', async () => {
    const cpuJob = makeJob({
      id: 'job-cpu',
      task_title: 'cpu benchmark',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      accelerator_type: null,
      polling_metadata: { result_metrics: { avg_latency_ms: 42.5, throughput_rps: 1000 } },
    });
    fetchComputeJobs.mockResolvedValue([cpuJob]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('cpu benchmark');
    expect(await findByText((t) => t.includes('avg_latency_ms:'))).toBeTruthy();
    expect(await findByText((t) => t.includes('throughput_rps:'))).toBeTruthy();
  });

  test('running job with result_metrics shows those metrics (not just terminal jobs)', async () => {
    const runningJobWithMetrics = makeJob({
      id: 'job-running',
      task_title: 'live experiment',
      status: 'RUNNING',
      outcome: null,
      accrued_cost_usd: 1.5,
      polling_metadata: { result_metrics: { current_progress: 0.75, samples_collected: 300 } },
    });
    fetchComputeJobs.mockResolvedValue([runningJobWithMetrics]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('live experiment');
    expect(await findByText((t) => t.includes('current_progress:'))).toBeTruthy();
    expect(await findByText((t) => t.includes('samples_collected:'))).toBeTruthy();
  });

  test('renders backend RPC diagnostic warning', async () => {
    // Active RUNNING job with a diagnostic: warning shows operation, attempt,
    // retry timing and error message, and the Kill control stays available.
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        compute_rpc_diagnostic: makeDiagnostic({
          operation: 'terminate',
          error_type: 'ConnectionError',
          error_message: 'compute backend unreachable',
          attempt_count: 3,
          next_retry_at: null,
          retry_after_seconds: 45,
        }),
      }),
    ]);
    const { findByText, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-rpc-diagnostic');
    expect(warning.textContent).toContain('terminate');
    expect(warning.textContent).toContain('attempt 3');
    expect(warning.textContent).toContain('retry after 45s');
    expect(warning.textContent).toContain('compute backend unreachable');
    // The warning does not replace the live controls.
    expect(await findByText('Kill')).toBeTruthy();
  });

  test('terminal job with diagnostic shows warning distinct from terminal label and keeps result', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 1.0,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
        compute_rpc_diagnostic: makeDiagnostic({ operation: 'cost', job_status: 'FAILED' }),
      }),
    ]);
    const { findByTestId, findByText, findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-rpc-diagnostic');
    expect(warning.textContent).toContain('cost');
    // Terminal label still rendered, separate from the amber warning block.
    await findByText('Failed');
    expect(warning.textContent).not.toContain('Failed');
    // Result link still present alongside the warning.
    await findByRole('link', { name: /result\.json/i });
  });

  test('renders diagnostic from legacy polling_metadata fallback', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        polling_metadata: {
          compute_rpc_diagnostic: makeDiagnostic({ operation: 'launch', error_message: 'quota exhausted' }),
        },
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-rpc-diagnostic');
    expect(warning.textContent).toContain('launch');
    expect(warning.textContent).toContain('quota exhausted');
  });

  test('no diagnostic warning when the API omits the field', async () => {
    fetchComputeJobs.mockResolvedValue([makeJob()]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-rpc-diagnostic')).toBeNull();
  });

  test('provisioning-phase terminal job shows vm_phase, sanitized cause and launch contract', async () => {
    // A $0 TIMED_OUT job that died before the VM reached RUNNING: the panel
    // surfaces the provisioning cause and the requested launch contract, and
    // suppresses the generic retry-oriented RPC warning.
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'TIMED_OUT',
        outcome: 'TIMED_OUT',
        final_cost_usd: 0,
        terminal_reason: 'compute job timed out while provisioning',
        vm_phase: 'provisioning',
        launch_contract: { requested_deadline_seconds: 600, provisioning_model: 'best_effort' },
        compute_rpc_diagnostic: makeDiagnostic({
          operation: 'launch',
          error_type: 'QuotaExceeded',
          error_message: 'GPU quota exhausted in zone us-central1-a',
        }),
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-provisioning-cause');
    expect(block.textContent).toContain('provisioning');
    expect(block.textContent).toContain('launch');
    expect(block.textContent).toContain('QuotaExceeded');
    expect(block.textContent).toContain('GPU quota exhausted in zone us-central1-a');
    expect(block.textContent).toContain('requested deadline 600s');
    expect(block.textContent).toContain('provisioning model best_effort');
    // The terminal_reason still renders, and the generic RPC warning is suppressed.
    expect(queryByTestId('compute-rpc-diagnostic')).toBeNull();
  });

  test('terminal job without provisioning fields falls back to terminal_reason only', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'TIMED_OUT',
        outcome: 'TIMED_OUT',
        final_cost_usd: 0,
        terminal_reason: 'compute job timed out while provisioning',
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute job timed out while provisioning');
    expect(queryByTestId('compute-provisioning-cause')).toBeNull();
  });

  test('kill failure surfaces the backend explanation, not a generic message', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    fetchComputeJobs.mockResolvedValue([makeJob()]);
    killComputeJob.mockRejectedValue(new Error('compute job already terminal: cannot kill'));
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const killBtn = await findByText('Kill');
    await act(async () => { killBtn.click(); });
    await waitFor(() => expect(killComputeJob).toHaveBeenCalledWith('job-1'));
    await findByText('compute job already terminal: cannot kill');
  });

  test('renders an empty compute workbench when there are no jobs', async () => {
    fetchComputeJobs.mockResolvedValue([]);
    const { findByText, queryByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByRole('button', { name: 'Launch compute experiment' })).toBeNull();
  });

  test('renders a launch action in the empty compute workbench when provided', async () => {
    fetchComputeJobs.mockResolvedValue([]);
    const { findByText, findByRole } = render(<ComputeJobsPanel projectId="proj-1" onLaunchCompute={vi.fn()} />);
    await findByText('No compute jobs');
    expect(await findByRole('button', { name: 'Launch compute experiment' })).toBeTruthy();
  });

  test('terminal job with command_log renders log text and truncated badge when truncated', async () => {
    const terminalJob = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      polling_metadata: {
        command_log: { text: 'step 1\nstep 2\nstep 3\ndone', truncated: false, byte_count: 24 },
      },
    });
    fetchComputeJobs.mockResolvedValue([terminalJob]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-command-log');
    expect(block.textContent).toContain('step 1');
    expect(block.textContent).toContain('done');
    expect(block.textContent).not.toContain('truncated');
  });

  test('terminal job with truncated command_log shows truncated badge', async () => {
    const terminalJob = makeJob({
      status: 'FAILED',
      outcome: 'FAILURE',
      final_cost_usd: 0.5,
      polling_metadata: {
        command_log: { text: 'error: something went wrong\nexit 1', truncated: true, byte_count: 65536 },
      },
    });
    fetchComputeJobs.mockResolvedValue([terminalJob]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-command-log');
    expect(block.textContent).toContain('truncated');
    expect(block.textContent).toContain('error: something went wrong');
  });

  test('active job does not render command_log section', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        polling_metadata: { command_log: { text: 'running...', truncated: false, byte_count: 10 } },
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-command-log')).toBeNull();
  });

  test('shows live output tail when job is running', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'RUNNING',
        polling_metadata: { live_output: { text: 'line1\nline2\n', truncated: false } },
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-live-output');
    expect(block.textContent).toContain('line1');
    // While the job is still active, the terminal command-log block is absent.
    expect(queryByTestId('compute-command-log')).toBeNull();
  });

  test('live output hidden when job is terminal and command_log shown', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: {
          live_output: { text: 'line1\nline2\n', truncated: false },
          command_log: { text: 'line1\nline2\ndone', truncated: false, byte_count: 16 },
        },
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const log = await findByTestId('compute-command-log');
    expect(log.textContent).toContain('done');
    // Once terminal, the live-output section disappears.
    expect(queryByTestId('compute-live-output')).toBeNull();
  });

  test('terminal job with exit_code in polling_metadata shows exit code chip', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 0.5,
        terminal_reason: 'command failed',
        polling_metadata: { exit_code: 1 },
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const chip = await findByTestId('compute-exit-code');
    expect(chip.textContent).toBe('exit 1');
  });

  test('active job does not show exit code chip even if present', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ polling_metadata: { exit_code: 0 } }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-exit-code')).toBeNull();
  });

  test('terminal job without exit_code shows no exit code chip', async () => {
    fetchComputeJobs.mockResolvedValue([makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 })]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-exit-code')).toBeNull();
  });

  test('terminal job with timestamps shows result story: runtime, deadline, cost rate', async () => {
    const start = new Date('2026-06-23T10:00:00Z');
    const end = new Date('2026-06-23T10:05:00Z'); // 300s runtime
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        started_at: start.toISOString(),
        completed_at: end.toISOString(),
        final_cost_usd: 0.20, // $0.20 in 300s => $2.40/hr
        launch_contract: { requested_deadline_seconds: 600, provisioning_model: 'SPOT' },
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const story = await findByTestId('compute-result-story');
    expect(story.textContent).toContain('5m');
    expect(story.textContent).toContain('10m');
    expect(story.textContent).toContain('budget');
    expect(story.textContent).toContain('$2.40/hr');
  });

  test('terminal job without timestamps shows no result story block', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        started_at: null,
        completed_at: null,
        final_cost_usd: 1.0,
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-result-story')).toBeNull();
  });

  test('active job does not show result story block', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        started_at: new Date('2026-06-23T10:00:00Z').toISOString(),
        completed_at: null,
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-result-story')).toBeNull();
  });

  test('provisioning model appears in the machine info subtitle', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ launch_contract: { provisioning_model: 'SPOT', requested_deadline_seconds: 300 } }),
    ]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const subtitle = await findByText((t) => t.includes('SPOT'));
    expect(subtitle.textContent).toContain('gcp_compute_engine');
    expect(subtitle.textContent).toContain('SPOT');
  });

  test('launch action calls onLaunchCompute from the empty compute workbench', async () => {
    const onLaunchCompute = vi.fn();
    fetchComputeJobs.mockResolvedValue([]);
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" onLaunchCompute={onLaunchCompute} />);
    const launchButton = await findByRole('button', { name: 'Launch compute experiment' });
    await act(async () => { launchButton.click(); });
    expect(onLaunchCompute).toHaveBeenCalledTimes(1);
  });

  test('run analysis fetches and shows the knowledge doc when an id is returned', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockResolvedValue({ status: 'upgraded', knowledge_document_id: 'doc-xyz' });
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-xyz',
      title: 'Sweep result: 87.3% accuracy',
      summary: 'The experiment achieved 87.3% accuracy at epoch 50.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const { findByRole, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    const docBlock = await findByTestId('compute-analysis-doc');
    expect(docBlock.textContent).toContain('Sweep result: 87.3% accuracy');
    expect(docBlock.textContent).toContain('87.3% accuracy at epoch 50');
    expect(fetchKnowledgeDocument).toHaveBeenCalledWith('proj-1', 'doc-xyz');
  });

  test('renders knowledge document link for terminal experiment', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-durable-1',
    });
    fetchComputeJobs.mockResolvedValue([terminal]);
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-durable-1',
      title: 'Experiment: sweep accuracy 91.2%',
      summary: 'Achieved 91.2% accuracy at epoch 40.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-knowledge-doc-link');
    expect(block.textContent).toContain('Experiment: sweep accuracy 91.2%');
    expect(block.textContent).toContain('Achieved 91.2% accuracy at epoch 40.');
    expect(fetchKnowledgeDocument).toHaveBeenCalledWith('proj-1', 'doc-durable-1');
  });

  test('knowledge doc card is interactive: clicking the title toggles the full body', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-expand-1',
    });
    fetchComputeJobs.mockResolvedValue([terminal]);
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-expand-1',
      title: 'Knowledge: full analysis write-up',
      summary: 'Short preview summary.',
      body: '# Full Document Body\n\nThis is the complete analysis with all the details that were previously inaccessible to the operator.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Initially collapsed — full body not shown
    await findByTestId('compute-knowledge-doc-toggle');
    expect(queryByTestId('compute-knowledge-doc-body')).toBeNull();
    // Click to expand
    const toggle = await findByTestId('compute-knowledge-doc-toggle');
    await act(async () => { toggle.click(); });
    // Full body is now visible
    const body = await findByTestId('compute-knowledge-doc-body');
    expect(body.textContent).toContain('This is the complete analysis with all the details');
  });

  test('analysis doc card is interactive: clicking the title toggles the full body', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockResolvedValue({ status: 'upgraded', knowledge_document_id: 'doc-analysis-expand' });
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-analysis-expand',
      title: 'Analysis: LLM grounded write-up',
      summary: 'Truncated preview.',
      body: '# Full Analysis\n\nThe complete LLM-grounded analysis body that the operator needs to read.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const { findByRole, findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    // Doc card appears after analysis completes
    await findByTestId('compute-analysis-doc');
    // Initially collapsed — full body not shown
    expect(queryByTestId('compute-analysis-doc-body')).toBeNull();
    // Click to expand
    const toggle = await findByTestId('compute-analysis-doc-toggle');
    await act(async () => { toggle.click(); });
    // Full body is now visible
    const body = await findByTestId('compute-analysis-doc-body');
    expect(body.textContent).toContain('The complete LLM-grounded analysis body');
  });

  test('terminal job with hypothesis and experiment shows experiment identity block', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        experiment: 'sweep-v3',
        hypothesis: 'Increasing learning rate to 0.01 will improve accuracy by 5%.',
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-experiment-identity');
    expect(block.textContent).toContain('sweep-v3');
    expect(block.textContent).toContain('Increasing learning rate to 0.01');
  });

  test('terminal job with benchmark (no experiment) shows Benchmark label', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 0.5,
        benchmark: 'mlperf-v4',
        hypothesis: null,
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-experiment-identity');
    expect(block.textContent).toContain('Benchmark');
    expect(block.textContent).toContain('mlperf-v4');
  });

  test('active job does not show experiment identity block even when fields present', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ experiment: 'sweep-v3', hypothesis: 'some hypothesis' }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-experiment-identity')).toBeNull();
  });

  test('terminal job without experiment/benchmark/hypothesis shows no identity block', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-experiment-identity')).toBeNull();
  });

  test('groups terminal jobs sharing an experiment identity into a sweep summary with best run highlighted and criterion count', async () => {
    const run1 = makeJob({
      id: 'job-run1',
      task_title: 'sweep run 1',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'lr-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.85, met: false, measurable: true },
    });
    const run2 = makeJob({
      id: 'job-run2',
      task_title: 'sweep run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'lr-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.93, met: true, measurable: true },
    });
    const run3 = makeJob({
      id: 'job-run3',
      task_title: 'sweep run 3',
      status: 'FAILED',
      outcome: 'FAILURE',
      final_cost_usd: 0.5,
      experiment: 'lr-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.91, met: true, measurable: true },
    });
    fetchComputeJobs.mockResolvedValue([run1, run2, run3]);
    const { findByTestId, getAllByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Sweep summary section appears
    await findByTestId('experiment-sweep-summary');
    // 2/3 met criterion
    expect(getByTestId('sweep-criterion-count').textContent).toBe('2/3 met criterion');
    // Best run (highest accuracy = 0.93 = run2) is highlighted
    const bestRow = getByTestId('sweep-best-run');
    expect(bestRow.textContent).toContain('sweep run 2');
    expect(bestRow.textContent).toContain('0.93');
    // Other runs appear as non-best rows
    const otherRows = getAllByTestId('sweep-run-row');
    expect(otherRows).toHaveLength(2);
  });

  test('sweep summary shows lower-is-better best run when operator is <=', async () => {
    const run1 = makeJob({
      id: 'job-lat1',
      task_title: 'latency run 1',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'latency-sweep',
      criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 95, met: true, measurable: true },
    });
    const run2 = makeJob({
      id: 'job-lat2',
      task_title: 'latency run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'latency-sweep',
      criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 80, met: true, measurable: true },
    });
    fetchComputeJobs.mockResolvedValue([run1, run2]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('experiment-sweep-summary');
    // Best run for latency (lower is better) should be run2 with 80ms
    const bestRow = await findByTestId('sweep-best-run');
    expect(bestRow.textContent).toContain('latency run 2');
    expect(bestRow.textContent).toContain('80');
  });

  test('single terminal job with experiment does not show sweep summary', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        experiment: 'lone-sweep',
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.91, met: true, measurable: true },
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('experiment-sweep-summary')).toBeNull();
  });

  test('running job projected to exceed spend cap before deadline shows a budget-kill warning', async () => {
    // Job started 1 hour ago, has accrued $3 of a $4 cap, deadline is 2h away.
    // Burn rate = $3/hr; projected additional = $3 * 2hr = $6; total projected = $9 > $4 cap.
    const startedAt = new Date(Date.now() - 3600 * 1000).toISOString();
    const deadline = new Date(Date.now() + 2 * 3600 * 1000).toISOString();
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'RUNNING',
        accrued_cost_usd: 3.0,
        spend_cap_usd: 4.0,
        started_at: startedAt,
        wall_clock_deadline: deadline,
      }),
    ]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-budget-kill-warning');
    expect(warning.textContent).toContain('$4.00');
    expect(warning.textContent).toContain('/hr');
  });

  test('running job on track to finish within budget shows no budget-kill warning', async () => {
    // Job started 1 hour ago, accrued $1 of $10 cap, deadline 1hr away.
    // Burn rate = $1/hr; projected total = $2 < $10 cap.
    const startedAt = new Date(Date.now() - 3600 * 1000).toISOString();
    const deadline = new Date(Date.now() + 3600 * 1000).toISOString();
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'RUNNING',
        accrued_cost_usd: 1.0,
        spend_cap_usd: 10.0,
        started_at: startedAt,
        wall_clock_deadline: deadline,
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-budget-kill-warning')).toBeNull();
  });

  test('renders run comparison matrix with per-metric best highlight', async () => {
    // job1: accuracy=0.85 (unmet), loss=0.3, cost=$1.00
    // job2: accuracy=0.93 (met), loss=0.2, cost=$2.00
    // criterion: accuracy >= 0.9 → higher-better → job2 is best
    // loss: no direction (unknown result_metric) → no best highlight
    // cost (__cost__): lower-better → job1 ($1.00) is best
    // runtime: both null (no timestamps) → no best highlight
    const job1 = makeJob({
      id: 'cmp-job1',
      task_title: 'run A',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'comparison-test',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.85, met: false, measurable: true },
      polling_metadata: { result_metrics: { accuracy: 0.85, loss: 0.3 } },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'cmp-job2',
      task_title: 'run B',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      experiment: 'comparison-test',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.93, met: true, measurable: true },
      polling_metadata: { result_metrics: { accuracy: 0.93, loss: 0.2 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue([job1, job2]);
    const { findByTestId, getAllByTestId, queryAllByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Sweep summary appears; open compare mode
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    // Matrix is shown
    const matrix = await findByTestId('run-comparison-matrix');
    expect(matrix).toBeTruthy();
    // Matrix unions metric keys across runs (accuracy from criterion + loss from result_metrics)
    expect(matrix.textContent).toContain('accuracy');
    expect(matrix.textContent).toContain('loss');
    // Cost row is present (known direction: lower-better)
    expect(matrix.textContent).toContain('Cost');
    // Best-per-metric cells: accuracy → run B (0.93), cost → run A ($1.00)
    const bestCells = getAllByTestId('matrix-best-cell');
    const bestTexts = bestCells.map((c) => c.textContent);
    // accuracy best = 0.93 (job2)
    expect(bestTexts).toContain('0.93');
    // cost best = $1.00 (job1)
    expect(bestTexts.some((t) => t?.includes('$1.00'))).toBe(true);
    // loss has no best-cell (unknown direction)
    // All matrix-best-cell entries should be for accuracy or cost, not loss
    // Get loss row: find the row containing 'loss' label and verify no matrix-best-cell in it
    const allRows = matrix.querySelectorAll('tbody tr');
    let lossRowHasBest = false;
    for (const row of allRows) {
      if (row.textContent?.includes('loss')) {
        lossRowHasBest = row.querySelector('[data-testid="matrix-best-cell"]') !== null;
      }
    }
    expect(lossRowHasBest).toBe(false);
    // Met/missed summary
    const verdictSummary = getByTestId('matrix-verdict-summary');
    expect(verdictSummary.textContent).toContain('met');
    expect(verdictSummary.textContent).toContain('missed');
    // Both runs appear as verdict chips
    const chips = queryAllByTestId('matrix-verdict-chip');
    expect(chips).toHaveLength(2);
  });

  test('active jobs are excluded from sweep summary groups', async () => {
    const activeJob = makeJob({ experiment: 'active-sweep', status: 'RUNNING', outcome: null });
    const terminalJob = makeJob({
      id: 'job-terminal',
      experiment: 'active-sweep',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
    });
    fetchComputeJobs.mockResolvedValue([activeJob, terminalJob]);
    const { findAllByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const titles = await findAllByText('compute_experiment: sweep');
    expect(titles.length).toBeGreaterThanOrEqual(1);
    // Only one terminal job in the group — no summary shown
    expect(queryByTestId('experiment-sweep-summary')).toBeNull();
  });

  test('run analysis without knowledge_document_id still shows feedback without crashing', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue([terminal]);
    analyzeComputeJob.mockResolvedValue({ status: 'upgraded', knowledge_document_id: null });
    const { findByRole, findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('Analysis complete');
    expect(fetchKnowledgeDocument).not.toHaveBeenCalled();
    expect(queryByTestId('compute-analysis-doc')).toBeNull();
  });

  test('terminal job with criterion verdict displays margin above threshold in green', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
      }),
    ]);
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('+0.05');
    expect(margin.classList.contains('text-emerald-300')).toBe(true);
  });

  test('terminal job with criterion verdict displays margin below threshold in red', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.85, met: false, measurable: true },
      }),
    ]);
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('-0.05');
    expect(margin.classList.contains('text-red-300')).toBe(true);
  });

  test('terminal job with <= operator shows correct margin sign', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'latency_ms', operator: '<=', threshold: 100 },
        criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 80, met: true, measurable: true },
      }),
    ]);
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('+20.00');
    expect(margin.classList.contains('text-emerald-300')).toBe(true);
  });

  test('terminal job without criterion_verdict does not show margin', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: null,
      }),
    ]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('not reported');
    expect(queryByTestId('criterion-margin')).toBeNull();
  });

  test('renders empty-state prompt when projectId is null', async () => {
    const { findByText, queryByRole } = render(<ComputeJobsPanel projectId={null} />);
    await findByText('Select a project to view compute experiments');
    expect(queryByRole('button', { name: 'Kill' })).toBeNull();
    expect(fetchComputeJobs).not.toHaveBeenCalled();
  });

  test('terminal job with null measured value does not show margin', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: null, met: false, measurable: false },
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    expect(queryByTestId('criterion-margin')).toBeNull();
  });

  test('window selector renders with 6h active by default', async () => {
    fetchComputeJobs.mockResolvedValue([]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const selector = await findByTestId('compute-window-selector');
    expect(selector.textContent).toContain('6h');
    expect(selector.textContent).toContain('24h');
    expect(selector.textContent).toContain('3d');
    expect(selector.textContent).toContain('7d');
    // Initially fetched with default (no window arg or default 360)
    expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', expect.anything(), 360);
  });

  test('clicking 7d window selector triggers refetch with 10080 minutes', async () => {
    fetchComputeJobs.mockResolvedValue([]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-window-selector');
    const btn7d = await findByTestId('compute-window-7d');
    await act(async () => { btn7d.click(); });
    await waitFor(() =>
      expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', expect.anything(), 10080),
    );
  });

  test('command log with more than 30 lines shows Show all button; clicking expands', async () => {
    const longLog = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n');
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { command_log: { text: longLog, truncated: false, byte_count: longLog.length } },
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const showAllBtn = await findByTestId('compute-log-show-all');
    expect(showAllBtn.textContent).toContain('50 lines');
    // Only last 30 lines visible — line 0 is not in the tail
    const logBlock = await findByTestId('compute-command-log');
    expect(logBlock.textContent).not.toContain('line 0');
    // Expand
    await act(async () => { showAllBtn.click(); });
    // All 50 lines now visible
    await waitFor(() => expect(findByTestId('compute-command-log')).resolves.toBeTruthy());
    const logBlockExpanded = await findByTestId('compute-command-log');
    expect(logBlockExpanded.textContent).toContain('line 0');
    // Show all button gone after expanding
    expect(queryByTestId('compute-log-show-all')).toBeNull();
  });

  test('command log with 30 or fewer lines shows no Show all button', async () => {
    const shortLog = Array.from({ length: 10 }, (_, i) => `step ${i}`).join('\n');
    fetchComputeJobs.mockResolvedValue([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { command_log: { text: shortLog, truncated: false, byte_count: shortLog.length } },
      }),
    ]);
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-command-log');
    expect(queryByTestId('compute-log-show-all')).toBeNull();
  });

  test('terminal job with arms in result_metrics does not render [object Object] in per-job metrics row', async () => {
    const armsJob = makeJob({
      id: 'job-arms',
      task_title: 'arms experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      polling_metadata: {
        result_metrics: {
          accuracy: 0.92,
          arms: {
            'arm-a': { lr: 0.01, val_acc: 0.90 },
            'arm-b': { lr: 0.001, val_acc: 0.92 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue([armsJob]);
    const { findByText, container, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('arms experiment');
    // '[object Object]' must not appear anywhere
    expect(container.textContent).not.toContain('[object Object]');
    // Scalar metric still visible
    expect(container.textContent).toContain('accuracy:');
    // Arms breakdown section is shown
    const armsBlock = await findByTestId('arms-breakdown');
    expect(armsBlock.textContent).toContain('arm-a');
    expect(armsBlock.textContent).toContain('arm-b');
    expect(armsBlock.textContent).toContain('val_acc:');
  });

  test('arms breakdown renders non-numeric per-arm context fields (label, note, log)', async () => {
    const armsJob = makeJob({
      id: 'job-arms-ctx',
      task_title: 'context fields experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-a': {
              label: 'high lr run',
              val_acc: 0.90,
              note: 'converged quickly but overfit at epoch 8',
              log: 'epoch 1: loss=0.9\nepoch 2: loss=0.5',
            },
            'arm-b': {
              val_acc: 0.93,
              status: 'best',
            },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue([armsJob]);
    const { findByTestId, findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const armsBlock = await findByTestId('arms-breakdown');
    // arm names present
    expect(armsBlock.textContent).toContain('arm-a');
    expect(armsBlock.textContent).toContain('arm-b');
    // numeric metric visible
    expect(armsBlock.textContent).toContain('val_acc:');
    // label rendered as subtitle (arm-label test id)
    const labels = await findAllByTestId('arm-label');
    expect(labels[0].textContent).toBe('high lr run');
    // note rendered as text block
    const notes = await findAllByTestId('arm-note');
    expect(notes[0].textContent).toContain('overfit at epoch 8');
    // log rendered as pre block
    const logs = await findAllByTestId('arm-log');
    expect(logs[0].textContent).toContain('epoch 1: loss=0.9');
    // other string field shown inline
    expect(armsBlock.textContent).toContain('status:');
    expect(armsBlock.textContent).toContain('best');
  });

  test('run comparison matrix skips arms key from scalar metric rows', async () => {
    const job1 = makeJob({
      id: 'arms-cmp-1',
      task_title: 'run X',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'arms-sweep',
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.88, met: false, measurable: true },
      polling_metadata: {
        result_metrics: {
          accuracy: 0.88,
          arms: { 'arm-x': { score: 0.88 } },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'arms-cmp-2',
      task_title: 'run Y',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      experiment: 'arms-sweep',
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.93, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          accuracy: 0.93,
          arms: { 'arm-y': { score: 0.93 } },
        },
      },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue([job1, job2]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const matrix = await findByTestId('run-comparison-matrix');
    // 'arms' must not appear as a row label in the scalar table
    const tbody = matrix.querySelector('tbody');
    const rowLabels = Array.from(tbody?.querySelectorAll('td:first-child') ?? []).map((td) => td.textContent);
    expect(rowLabels).not.toContain('arms');
    // numeric accuracy row still present
    expect(rowLabels).toContain('accuracy');
  });

  test('run comparison matrix renders arms-comparison section when any job carries arms', async () => {
    const job1 = makeJob({
      id: 'arms-matrix-1',
      task_title: 'matrix run 1',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'matrix-arms-test',
      criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.8, value: 0.82, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          score: 0.82,
          arms: {
            'control': { precision: 0.80, recall: 0.85 },
            'treatment': { precision: 0.84, recall: 0.87 },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'arms-matrix-2',
      task_title: 'matrix run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.2,
      experiment: 'matrix-arms-test',
      criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.8, value: 0.79, met: false, measurable: true },
      polling_metadata: { result_metrics: { score: 0.79 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue([job1, job2]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const armsSection = await findByTestId('arms-comparison');
    expect(armsSection.textContent).toContain('Per-arm results');
    expect(armsSection.textContent).toContain('control');
    expect(armsSection.textContent).toContain('treatment');
    expect(armsSection.textContent).toContain('precision:');
    expect(armsSection.textContent).toContain('recall:');
  });

  test('arms comparison section shows per-arm metric key-value pairs for array-shape arms', async () => {
    const job1 = makeJob({
      id: 'arms-array-1',
      task_title: 'array arms run 1',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'array-arms-sweep',
      criterion_verdict: { metric: 'f1', operator: '>=', threshold: 0.75, value: 0.77, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          f1: 0.77,
          arms: [
            { name: 'baseline', f1: 0.72, auc: 0.81 },
            { name: 'experimental', f1: 0.77, auc: 0.85 },
          ],
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'arms-array-2',
      task_title: 'array arms run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 0.8,
      experiment: 'array-arms-sweep',
      criterion_verdict: { metric: 'f1', operator: '>=', threshold: 0.75, value: 0.74, met: false, measurable: true },
      polling_metadata: { result_metrics: { f1: 0.74 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue([job1, job2]);
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const armsSection = await findByTestId('arms-comparison');
    expect(armsSection.textContent).toContain('baseline');
    expect(armsSection.textContent).toContain('experimental');
    expect(armsSection.textContent).toContain('auc:');
    expect(armsSection.textContent).toContain('0.85');
  });

  test('job with external_compute_id shows it in the machine info subtitle', async () => {
    fetchComputeJobs.mockResolvedValue([
      makeJob({ external_compute_id: 'compute-job-abc-123' }),
    ]);
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const subtitle = await findByText((t) => t.includes('compute-job-abc-123'));
    expect(subtitle.textContent).toContain('gcp_compute_engine');
    expect(subtitle.textContent).toContain('compute-job-abc-123');
  });
});
