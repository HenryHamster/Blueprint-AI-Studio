import { describe, test, expect, vi, afterEach } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';
import type { ComputeJob } from '../api';
import { ComputeJobsPanel } from './ComputeJobsPanel';
import {
  isComputeJobActive,
  computeJobTerminalLabel,
  computeJobRpcDiagnostic,
  fmtCountdown,
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
});
