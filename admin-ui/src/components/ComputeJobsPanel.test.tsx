import { describe, test, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, waitFor, act, fireEvent, within } from '@testing-library/react';
import type { ComputeJob, ComputeJobsListResponse } from '../api';
import { ComputeJobsPanel, CompactJobsTable } from './ComputeJobsPanel';
import {
  isComputeJobActive,
  computeJobTerminalLabel,
  computeJobRpcDiagnostic,
  computeJobRuntimeSec,
  computeJobMargin,
  computeJobExecutionLocation,
  computeJobEvidenceTier,
  computeJobImagePinStatus,
  fmtCountdown,
  fmtDuration,
} from './ComputeJobsPanel.utils';
import type { ComputeRpcDiagnostic } from '../api';

const fetchComputeJobs = vi.fn();
const killComputeJob = vi.fn();
const analyzeComputeJob = vi.fn();
const fetchKnowledgeDocument = vi.fn();
const resolveArtifactDownloadUrl = vi.fn();
const fetchComputeJobsAggregateSummary = vi.fn();
const fetchHeldComputeExperiments = vi.fn();
const fetchAwaitingLocalRunnerHolds = vi.fn();
const fetchComputeSweepGroups = vi.fn();
const rerunComputeExperiment = vi.fn();
const askComputeJob = vi.fn();

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    fetchComputeJobs: (...args: unknown[]) => fetchComputeJobs(...args),
    fetchComputeJobsPaged: async (...args: unknown[]) => {
      const r = await fetchComputeJobs(...args);
      return Array.isArray(r) ? { jobs: r, hasMore: false } : r;
    },
    killComputeJob: (...args: unknown[]) => killComputeJob(...args),
    analyzeComputeJob: (...args: unknown[]) => analyzeComputeJob(...args),
    fetchKnowledgeDocument: (...args: unknown[]) => fetchKnowledgeDocument(...args),
    resolveArtifactDownloadUrl: (...args: unknown[]) => resolveArtifactDownloadUrl(...args),
    fetchComputeJobsAggregateSummary: (...args: unknown[]) => fetchComputeJobsAggregateSummary(...args),
    fetchHeldComputeExperiments: (...args: unknown[]) => fetchHeldComputeExperiments(...args),
    fetchAwaitingLocalRunnerHolds: (...args: unknown[]) => fetchAwaitingLocalRunnerHolds(...args),
    fetchComputeSweepGroups: (...args: unknown[]) => fetchComputeSweepGroups(...args),
    rerunComputeExperiment: (...args: unknown[]) => rerunComputeExperiment(...args),
    askComputeJob: (...args: unknown[]) => askComputeJob(...args),
  });
});

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

function wrapJobs(jobs: ComputeJob[], hasMore = false): ComputeJobsListResponse {
  return {
    jobs,
    hasMore,
    summary: {
      total_jobs: jobs.length,
      active_jobs: 0,
      terminal_jobs: jobs.length,
      total_spend_usd: 0,
      outcome_breakdown: {},
      criterion_measured: 0,
      criterion_met: 0,
    },
    limit: 50,
    offset: 0,
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

  test('computeJobTerminalLabel returns Killed: no progress when terminal_kind is no_progress', () => {
    expect(
      computeJobTerminalLabel(makeJob({ outcome: 'TIMED_OUT', terminal_kind: 'no_progress' })),
    ).toBe('Killed: no progress');
  });

  test('computeJobTerminalLabel returns Killed: deadline for TIMED_OUT with terminal_kind deadline', () => {
    expect(
      computeJobTerminalLabel(makeJob({ outcome: 'TIMED_OUT', terminal_kind: 'deadline' })),
    ).toBe('Killed: deadline');
  });

  test('computeJobTerminalLabel returns Killed: deadline for TIMED_OUT with absent terminal_kind (backward compat)', () => {
    expect(
      computeJobTerminalLabel(makeJob({ outcome: 'TIMED_OUT' })),
    ).toBe('Killed: deadline');
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

  test('computeJobExecutionLocation maps known local backends to operator-hardware', () => {
    expect(computeJobExecutionLocation('local-subprocess')).toEqual({ label: 'Operator hardware', isOperatorHardware: true });
    expect(computeJobExecutionLocation('local-gpu')).toEqual({ label: 'Operator hardware · GPU', isOperatorHardware: true });
    expect(computeJobExecutionLocation('runner-subprocess')).toEqual({ label: 'Operator hardware · Runner', isOperatorHardware: true });
  });

  test('computeJobExecutionLocation maps gcp to GCP cloud', () => {
    expect(computeJobExecutionLocation('gcp')).toEqual({ label: 'GCP cloud', isOperatorHardware: false });
  });

  test('computeJobExecutionLocation falls back to raw string for unknown backends', () => {
    expect(computeJobExecutionLocation('gcp_compute_engine')).toEqual({ label: 'gcp_compute_engine', isOperatorHardware: false });
    expect(computeJobExecutionLocation('unknown-backend')).toEqual({ label: 'unknown-backend', isOperatorHardware: false });
  });

  test('computeJobEvidenceTier: operator hardware + a result → covered', () => {
    const result = { armA: { value: 1.5, met: true, cost_usd: null } };
    expect(
      computeJobEvidenceTier(makeJob({ backend: 'local-subprocess', per_arm_verdicts: result })),
    ).toBe('covered');
    expect(
      computeJobEvidenceTier(makeJob({ backend: 'runner-subprocess', per_arm_verdicts: result })),
    ).toBe('covered');
    expect(
      computeJobEvidenceTier(makeJob({ backend: 'local-gpu', per_arm_verdicts: result })),
    ).toBe('covered');
  });

  test('computeJobEvidenceTier: operator hardware with NO result is not machine-verified', () => {
    // `covered` renders as "Machine verified". Deriving it from the backend name
    // alone made it a LOCATION proxy — an empty run claimed verification it never
    // earned. Provenance is real (so not `uncovered`), but nothing was proved.
    expect(computeJobEvidenceTier(makeJob({ backend: 'local-subprocess' }))).toBe('partial');
    expect(computeJobEvidenceTier(makeJob({ backend: 'runner-subprocess' }))).toBe('partial');
    expect(computeJobEvidenceTier(makeJob({ backend: 'local-gpu' }))).toBe('partial');
  });

  test('computeJobEvidenceTier: gcp → attested', () => {
    expect(computeJobEvidenceTier(makeJob({ backend: 'gcp' }))).toBe('attested');
    expect(computeJobEvidenceTier(makeJob({ backend: 'gcp_compute_engine' }))).toBe('attested');
  });

  test('computeJobImagePinStatus: mutable tag → mutable', () => {
    expect(
      computeJobImagePinStatus(makeJob({ launch_params: { container_image: 'ghcr.io/org/base:latest' } })),
    ).toBe('mutable');
  });

  test('computeJobImagePinStatus: digest-pinned ref → pinned', () => {
    expect(
      computeJobImagePinStatus(
        makeJob({ launch_params: { container_image: `ghcr.io/org/base@sha256:${'a'.repeat(64)}` } }),
      ),
    ).toBe('pinned');
  });

  test('computeJobImagePinStatus: no image / no launch_params → null', () => {
    expect(computeJobImagePinStatus(makeJob({ launch_params: { command: 'python x.py' } }))).toBeNull();
    expect(computeJobImagePinStatus(makeJob({ launch_params: undefined }))).toBeNull();
  });
});

describe('ComputeJobsPanel evidence-tier badge rendering', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    fetchComputeJobsAggregateSummary.mockReset();
    fetchHeldComputeExperiments.mockReset();
    fetchAwaitingLocalRunnerHolds.mockReset();
    fetchComputeSweepGroups.mockReset();
  });

  test('renders evidence-tier badge on terminal operator-hardware job', async () => {
    const job = makeJob({
      backend: 'local-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      // A measurable result is REQUIRED for "Machine verified" — operator hardware
      // alone only says where it ran. Without this the job is `partial`.
      per_arm_verdicts: { armA: { value: 0.91, met: true, cost_usd: null } },
      accrued_cost_usd: 0,
      final_cost_usd: 0,
      completed_at: new Date().toISOString(),
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { getByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    // Scope to the jobs list: the panel's tier FILTER control also renders the
    // text 'Machine verified', so an unscoped query would be ambiguous.
    await waitFor(() => {
      expect(within(getByTestId('compute-jobs-list')).getByText('Machine verified')).toBeTruthy();
    });
  });

  test('active job renders no evidence-tier badge', async () => {
    const job = makeJob({
      backend: 'local-subprocess',
      status: 'RUNNING',
      outcome: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { findByText, getByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    // Wait for the active job to render, then confirm no tier badge in the jobs
    // list (scoped so the filter control's 'Machine verified' label is excluded).
    await findByText('compute_experiment: sweep');
    expect(within(getByTestId('compute-jobs-list')).queryByText('Machine verified')).toBeNull();
  });

  test('compact jobs table renders evidence-tier badge and runner id for terminal jobs', async () => {
    const job = makeJob({
      backend: 'local-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      per_arm_verdicts: { armA: { value: 0.91, met: true, cost_usd: null } },
      accrued_cost_usd: 0,
      final_cost_usd: 0,
      completed_at: new Date().toISOString(),
      runner: { id: 'dd59526e-0001-0002-0003-000000000004', hostname: 'iconlab-gpu-01', status: 'active' },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { findByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    // Wait for the job's tier badge to render in the jobs list (scoped past the
    // filter control which also contains a 'Machine verified' label), then switch
    // to compact table view.
    const jobsList = await findByTestId('compute-jobs-list');
    await waitFor(() => {
      expect(within(jobsList).getByText('Machine verified')).toBeTruthy();
    });
    const toggleBtn = await findByTestId('compute-view-mode-toggle');
    await act(async () => { toggleBtn.click(); });
    // Table should show evidence badge and runner hostname
    const table = await findByTestId('compact-jobs-table');
    expect(table.textContent).toContain('Machine verified');
    expect(table.textContent).toContain('iconlab-gpu-01');
  });

  test('renders omitted-file note when viewable artifacts were dropped', async () => {
    const job = makeJob({
      backend: 'local-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      completed_at: new Date().toISOString(),
      viewable_artifacts: [
        {
          filename: 'plot.png',
          storage_uri: 'gs://bucket/jobs/job-1/plot.png',
          content_type: 'image/png',
          size_bytes: 1024,
          kind: 'image',
        },
      ],
      viewable_artifacts_dropped_count: 3,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const note = await findByTestId('compute-artifact-omitted-note');
    expect(note.textContent).toContain('3 files omitted');
  });

  test('renders no omitted-file note when dropped count is zero', async () => {
    const job = makeJob({
      backend: 'local-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      completed_at: new Date().toISOString(),
      viewable_artifacts: [
        {
          filename: 'plot.png',
          storage_uri: 'gs://bucket/jobs/job-1/plot.png',
          content_type: 'image/png',
          size_bytes: 1024,
          kind: 'image',
        },
      ],
      viewable_artifacts_dropped_count: 0,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Gallery renders (artifact present) but no omitted note.
    await findByTestId('compute-artifact-gallery');
    expect(
      document.querySelector('[data-testid="compute-artifact-omitted-note"]'),
    ).toBeNull();
  });

  test('renders Mutable image badge for a job whose image lacks a digest', async () => {
    const job = makeJob({
      backend: 'gcp',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      completed_at: new Date().toISOString(),
      launch_params: { container_image: 'ghcr.io/org/base:latest' },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { getByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await waitFor(() => {
      expect(getByText('Mutable image')).toBeTruthy();
    });
  });

  test('no Mutable image badge for a digest-pinned image', async () => {
    const job = makeJob({
      backend: 'gcp',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      completed_at: new Date().toISOString(),
      launch_params: { container_image: `ghcr.io/org/base@sha256:${'a'.repeat(64)}` },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job]));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    const { queryByText, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Wait for the job row to render, then confirm no mutable-image warning.
    await findByText('compute_experiment: sweep');
    expect(queryByText('Mutable image')).toBeNull();
  });

  test('run surface earned-vs-claimed evidence tier never hardcodes machine-verified for cloud jobs', async () => {
    const setupMocks = () => {
      fetchComputeJobsAggregateSummary.mockResolvedValue(null);
      fetchHeldComputeExperiments.mockResolvedValue([]);
      fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
      fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    };

    // Cloud (gcp) job: must show 'Attested', must NOT show 'Machine verified'
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({
      id: 'job-gcp',
      backend: 'gcp',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      completed_at: new Date().toISOString(),
    })]));
    const { getByText: getGcp, getByTestId: gtGcp, unmount: unmountGcp } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    await waitFor(() => {
      expect(getGcp('Attested')).toBeTruthy();
    });
    // Scope 'Machine verified' past the tier filter control, which carries that label.
    expect(within(gtGcp('compute-jobs-list')).queryByText('Machine verified')).toBeNull();
    unmountGcp();

    // Operator-hardware (runner-subprocess) job WITH a measurable result: must
    // show 'Machine verified'. Post-#14597 `covered` requires BOTH operator
    // hardware AND a produced result — a bare local run only proves WHERE it ran
    // (see computeJobEvidenceTier), so the result is what earns the badge.
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({
      id: 'job-runner',
      task_title: 'compute_experiment: runner',
      backend: 'runner-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 0,
      completed_at: new Date().toISOString(),
      per_arm_verdicts: { armA: { value: 0.91, met: true, cost_usd: null } },
    })]));
    const { getByTestId: gtRunner, unmount: unmountRunner } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    await waitFor(() => {
      expect(within(gtRunner('compute-jobs-list')).getByText('Machine verified')).toBeTruthy();
    });
    unmountRunner();

    // No-backend job: must show 'Self-reported', must NOT show 'Machine verified'
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({
      id: 'job-no-backend',
      task_title: 'compute_experiment: no-backend',
      backend: null as unknown as string,
      status: 'FAILED',
      outcome: 'FAILURE',
      final_cost_usd: 0,
      completed_at: new Date().toISOString(),
    })]));
    const { getByText: getNoBackend, getByTestId: gtNoBackend, unmount: unmountNoBackend } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    await waitFor(() => {
      expect(getNoBackend('Self-reported')).toBeTruthy();
    });
    expect(within(gtNoBackend('compute-jobs-list')).queryByText('Machine verified')).toBeNull();
    unmountNoBackend();

    // Active cloud job: no evidence-tier badge at all
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({
      id: 'job-active-gcp',
      task_title: 'compute_experiment: active',
      backend: 'gcp',
      status: 'RUNNING',
      outcome: null,
    })]));
    const { findByText: findActive, getByTestId: gtActive } = render(
      <ComputeJobsPanel projectId="proj-1" />
    );
    await findActive('compute_experiment: active');
    // No evidence-tier badge anywhere in the jobs list for an active job (scoped
    // so the filter control's tier labels are excluded).
    const activeList = within(gtActive('compute-jobs-list'));
    expect(activeList.queryByText('Machine verified')).toBeNull();
    expect(activeList.queryByText('Attested')).toBeNull();
    expect(activeList.queryByText('Self-reported')).toBeNull();
  });
});

describe('ComputeJobsPanel watchdog protection status', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    fetchComputeJobsAggregateSummary.mockReset();
    fetchHeldComputeExperiments.mockReset();
    fetchAwaitingLocalRunnerHolds.mockReset();
    fetchComputeSweepGroups.mockReset();
  });

  function setupMocks() {
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
  }

  test('active job with watchdog_active:true renders Protected badge with timeout', async () => {
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        watchdog_active: true,
        no_progress_timeout_seconds: 1800,
        last_progress_at: new Date(Date.now() - 120_000).toISOString(),
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const el = await findByTestId('compute-watchdog-status');
    expect(el.querySelector('[data-testid="compute-watchdog-protected"]')).toBeTruthy();
    expect(el.textContent).toContain('Protected');
    expect(el.textContent).toContain('30m');
    expect(el.textContent).toContain('last progress');
  });

  test('active job with watchdog_active:false and no inert reason renders Unprotected badge', async () => {
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        watchdog_active: false,
        no_progress_timeout_seconds: null,
        watchdog_inert_reason: null,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const el = await findByTestId('compute-watchdog-status');
    expect(el.querySelector('[data-testid="compute-watchdog-unprotected"]')).toBeTruthy();
    expect(el.textContent).toContain('Unprotected');
    expect(el.querySelector('[data-testid="compute-watchdog-inert-warning"]')).toBeNull();
  });

  test('active job with watchdog_active:false and inert reason renders warning block', async () => {
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        watchdog_active: false,
        watchdog_inert_reason: 'no_declared_metric',
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const el = await findByTestId('compute-watchdog-status');
    const warning = el.querySelector('[data-testid="compute-watchdog-inert-warning"]');
    expect(warning).toBeTruthy();
    expect(warning?.textContent).toContain('no_declared_metric');
    expect(warning?.textContent).toContain('Watchdog will NOT cut this job');
  });

  test('active job without watchdog fields renders no watchdog indicator (backward compat)', async () => {
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({ status: 'RUNNING' })]));
    const { findByText, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(container.querySelector('[data-testid="compute-watchdog-status"]')).toBeNull();
  });

  test('terminal job does not render watchdog indicator even when fields are present', async () => {
    setupMocks();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        completed_at: new Date().toISOString(),
        watchdog_active: true,
        no_progress_timeout_seconds: 1800,
      }),
    ]));
    const { findByText, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(container.querySelector('[data-testid="compute-watchdog-status"]')).toBeNull();
  });
});

describe('ComputeJobsPanel render + kill', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    killComputeJob.mockReset();
    analyzeComputeJob.mockReset();
    fetchKnowledgeDocument.mockReset();
    resolveArtifactDownloadUrl.mockReset();
    fetchComputeJobsAggregateSummary.mockReset();
    fetchHeldComputeExperiments.mockReset();
    fetchAwaitingLocalRunnerHolds.mockReset();
    fetchComputeSweepGroups.mockReset();
    rerunComputeExperiment.mockReset();
    askComputeJob.mockReset();
    vi.restoreAllMocks();
    // Clear persisted ask state between tests to prevent cross-test contamination
    localStorage.removeItem('blueprint-admin:compute-ask-state:proj-1');
    localStorage.removeItem('blueprint-admin:compute-ask-state:test-proj-persist');
  });

  test('renders budget bar and task title for an active job', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({ accrued_cost_usd: 2.0, spend_cap_usd: 4.0 })]));
    const { findByText, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    // Budget readout reflects spent / cap.
    expect(container.textContent).toContain('$2.00 / $4.00');
    // Active job exposes a Kill control.
    expect(await findByText('Kill')).toBeTruthy();
  });

  test('shows a live-proof indicator when polling_metadata carries live_release_evidence', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        polling_metadata: {
          live_release_evidence: {
            release_version_number: 8,
            promoted_sha: 'abcdef1234567890deadbeef',
            aggregate_serving_status: 'serving_confirmed',
          },
        },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const chip = await findByTestId('release-evidence-chip');
    expect(chip.textContent).toContain('Live proof of v8');
    // Short SHA only — first 8 chars, not the full hash.
    expect(chip.textContent).toContain('abcdef12');
    expect(chip.textContent).not.toContain('abcdef1234567890deadbeef');
  });

  test('shows no live-proof indicator when live_release_evidence is absent', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob()]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('release-evidence-chip')).toBeNull();
  });

  test('calls onNavigateToTask when task title is clicked', async () => {
    const onNavigate = vi.fn();
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({ accrued_cost_usd: 2.0, spend_cap_usd: 4.0 })]));
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" onNavigateToTask={onNavigate} />);
    const taskTitle = await findByText('compute_experiment: sweep');
    await act(async () => { taskTitle.click(); });
    expect(onNavigate).toHaveBeenCalledWith('task-1');
  });

  test('kill button calls killComputeJob and shows killed reason', async () => {
    const active = makeJob();
    fetchComputeJobs.mockResolvedValue(wrapJobs([active]));
    killComputeJob.mockResolvedValue(
      makeJob({ status: 'CANCELLED', outcome: 'CANCELLED', terminal_reason: 'killed by operator (a@b.com)', final_cost_usd: 1.0 }),
    );
    const { findByText, findByTestId, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const killBtn = await findByText('Kill');
    await act(async () => { killBtn.click(); });
    // ConfirmDialog should appear; confirm it
    const confirmBtn = await findByTestId('confirm-dialog-confirm');
    await act(async () => { confirmBtn.click(); });
    await waitFor(() => expect(killComputeJob).toHaveBeenCalledWith('job-1'));
    await findByText('Killed by operator');
    expect(queryByText('Kill')).toBeNull();
  });

  test('run analysis shows upgraded feedback and refreshes the job list', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    analyzeComputeJob.mockResolvedValue({ status: 'analysis_unavailable', reason: 'analysis-disabled' });
    const { findByRole, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('Analysis unavailable: Analysis disabled');
  });

  test('run analysis surfaces backend errors inline', async () => {
    const terminal = makeJob({ status: 'FAILED', outcome: 'FAILURE', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    analyzeComputeJob.mockRejectedValue(new Error('analysis endpoint failed: upstream 503'));
    const { findByRole, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('analysis endpoint failed: upstream 503');
  });

  test('renders a safe terminal result link', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        accrued_cost_usd: 1.0,
        final_cost_usd: 1.0,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
      }),
    ]));
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    const link = await findByRole('link', { name: /https:\/\/storage\.example\.com\/jobs\/job-1\/result\.json/i });
    expect(link).toHaveAttribute('href', 'https://storage.example.com/jobs/job-1/result.json');
  });

  test('resolves a gs:// terminal result link through the admin API before opening', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue({ opener: null, location: { replace: vi.fn() }, close: vi.fn() } as never);
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed.example.com/jobs/job-1/result.json');
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        result_reference: 'gs://blueprint-agent-artifacts/tasks/task-1/result.json',
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        artifact_id: 'artifact-abc',
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        artifact_id: 'artifact-abc',
        result_reference: null,
      }),
    ]));
    const { findByText, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const artifact = await findByText('COMPUTE_RESULT artifact');
    // Present but not an openable link (no reference to resolve).
    expect(artifact.closest('a')).toBeNull();
    expect(artifact.closest('button')).toBeNull();
    expect(queryByText('Result reference')).toBeNull();
  });

  test('shows no artifact affordance for jobs without artifact_id', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
    ]));
    const { findByText, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText(/compute_experiment: sweep/i);
    expect(queryByText('COMPUTE_RESULT artifact')).toBeNull();
  });

  test('GPU job shows accelerator label alongside machine type', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        accelerator_type: 'T4',
        accelerator_count: 1,
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([gpuJob]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([cpuJob]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([runningJobWithMetrics]));
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('live experiment');
    expect(await findByText((t) => t.includes('current_progress:'))).toBeTruthy();
    expect(await findByText((t) => t.includes('samples_collected:'))).toBeTruthy();
  });

  test('renders backend RPC diagnostic warning', async () => {
    // Active RUNNING job with a diagnostic: warning shows operation, attempt,
    // retry timing and error message, and the Kill control stays available.
    fetchComputeJobs.mockResolvedValue(wrapJobs([
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
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 1.0,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
        compute_rpc_diagnostic: makeDiagnostic({ operation: 'cost', job_status: 'FAILED' }),
      }),
    ]));
    const { findByTestId, findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-rpc-diagnostic');
    expect(warning.textContent).toContain('cost');
    // Terminal label still rendered in the jobs list, separate from the amber warning block.
    const jobsList = await findByTestId('compute-jobs-list');
    expect(jobsList.textContent).toContain('Failed');
    expect(warning.textContent).not.toContain('Failed');
    // Result link still present alongside the warning.
    await findByRole('link', { name: /result\.json/i });
  });

  test('renders diagnostic from legacy polling_metadata fallback', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        polling_metadata: {
          compute_rpc_diagnostic: makeDiagnostic({ operation: 'launch', error_message: 'quota exhausted' }),
        },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const warning = await findByTestId('compute-rpc-diagnostic');
    expect(warning.textContent).toContain('launch');
    expect(warning.textContent).toContain('quota exhausted');
  });

  test('no diagnostic warning when the API omits the field', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob()]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-rpc-diagnostic')).toBeNull();
  });

  test('provisioning-phase terminal job shows vm_phase, sanitized cause and launch contract', async () => {
    // A $0 TIMED_OUT job that died before the VM reached RUNNING: the panel
    // surfaces the provisioning cause and the requested launch contract, and
    // suppresses the generic retry-oriented RPC warning.
    fetchComputeJobs.mockResolvedValue(wrapJobs([
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
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'TIMED_OUT',
        outcome: 'TIMED_OUT',
        final_cost_usd: 0,
        terminal_reason: 'compute job timed out while provisioning',
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute job timed out while provisioning');
    expect(queryByTestId('compute-provisioning-cause')).toBeNull();
  });

  test('kill failure surfaces the backend explanation, not a generic message', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob()]));
    killComputeJob.mockRejectedValue(new Error('compute job already terminal: cannot kill'));
    const { findByText, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const killBtn = await findByText('Kill');
    await act(async () => { killBtn.click(); });
    const confirmBtn = await findByTestId('confirm-dialog-confirm');
    await act(async () => { confirmBtn.click(); });
    await waitFor(() => expect(killComputeJob).toHaveBeenCalledWith('job-1'));
    await findByText('compute job already terminal: cannot kill');
  });

  test('renders an empty compute workbench when there are no jobs', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByText, queryByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByRole('button', { name: 'Launch experiment' })).toBeNull();
  });

  test('renders a launch action in the empty compute workbench when provided', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByText, findByRole } = render(<ComputeJobsPanel projectId="proj-1" onLaunchCompute={vi.fn()} />);
    await findByText('No compute jobs');
    expect(await findByRole('button', { name: 'Launch experiment' })).toBeTruthy();
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminalJob]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminalJob]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-command-log');
    expect(block.textContent).toContain('truncated');
    expect(block.textContent).toContain('error: something went wrong');
  });

  test('active job does not render command_log section', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        polling_metadata: { command_log: { text: 'running...', truncated: false, byte_count: 10 } },
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-command-log')).toBeNull();
  });

  test('shows live output tail when job is running', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        polling_metadata: { live_output: { text: 'line1\nline2\n', truncated: false } },
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-live-output');
    expect(block.textContent).toContain('line1');
    // While the job is still active, the terminal command-log block is absent.
    expect(queryByTestId('compute-command-log')).toBeNull();
  });

  test('live output hidden when job is terminal and command_log shown', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: {
          live_output: { text: 'line1\nline2\n', truncated: false },
          command_log: { text: 'line1\nline2\ndone', truncated: false, byte_count: 16 },
        },
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const log = await findByTestId('compute-command-log');
    expect(log.textContent).toContain('done');
    // Once terminal, the live-output section disappears.
    expect(queryByTestId('compute-live-output')).toBeNull();
  });

  test('renders progress series block with metric name, latest value, and sample count', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        progress_series: [
          { t: '2026-07-01T10:00:00Z', name: 'reward', value: 0.1 },
          { t: '2026-07-01T10:01:00Z', name: 'reward', value: 0.3 },
          { t: '2026-07-01T10:02:00Z', name: 'reward', value: 0.5 },
        ],
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-progress-series');
    expect(block.textContent).toContain('reward');
    expect(block.textContent).toContain('0.5');
    expect(block.textContent).toContain('3 samples');
  });

  test('no-progress cut tag appears on stalled terminal job with terminal_kind=no_progress', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'TIMED_OUT',
        outcome: 'TIMED_OUT',
        final_cost_usd: 0.5,
        terminal_kind: 'no_progress',
        progress_series: [
          { t: '2026-07-01T10:00:00Z', name: 'loss', value: 1.0 },
          { t: '2026-07-01T10:05:00Z', name: 'loss', value: 1.0 },
        ],
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-progress-series');
    expect(block.textContent).toContain('no-progress cut');
    expect(block.textContent).toContain('loss');
  });

  test('absent progress_series renders no progress block', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob()]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-progress-series')).toBeNull();
  });

  test('terminal job with exit_code shows exit code chip', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 0.5,
        terminal_reason: 'command failed',
        exit_code: 1,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const chip = await findByTestId('compute-exit-code');
    expect(chip.textContent).toBe('exit 1');
  });

  test('active job does not show exit code chip even if present', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ exit_code: 0 }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-exit-code')).toBeNull();
  });

  test('terminal job without exit_code shows no exit code chip', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 })]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-exit-code')).toBeNull();
  });

  test('terminal job with timestamps shows result story: runtime, deadline, cost rate', async () => {
    const start = new Date('2026-06-23T10:00:00Z');
    const end = new Date('2026-06-23T10:05:00Z'); // 300s runtime
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        started_at: start.toISOString(),
        completed_at: end.toISOString(),
        final_cost_usd: 0.20, // $0.20 in 300s => $2.40/hr
        launch_contract: { requested_deadline_seconds: 600, provisioning_model: 'SPOT' },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const story = await findByTestId('compute-result-story');
    expect(story.textContent).toContain('5m');
    expect(story.textContent).toContain('10m');
    expect(story.textContent).toContain('budget');
    expect(story.textContent).toContain('$2.40/hr');
  });

  test('terminal job without timestamps shows no result story block', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        started_at: null,
        completed_at: null,
        final_cost_usd: 1.0,
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-result-story')).toBeNull();
  });

  test('active job does not show result story block', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        started_at: new Date('2026-06-23T10:00:00Z').toISOString(),
        completed_at: null,
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-result-story')).toBeNull();
  });

  test('provisioning model appears in the machine info subtitle', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ launch_contract: { provisioning_model: 'SPOT', requested_deadline_seconds: 300 } }),
    ]));
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const subtitle = await findByText((t) => t.includes('SPOT'));
    expect(subtitle.textContent).toContain('gcp_compute_engine');
    expect(subtitle.textContent).toContain('SPOT');
  });

  test('clone-to-form button appears on terminal jobs and calls onCloneExperiment with mapped form fields', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      experiment: 'My Experiment',
      hypothesis: 'test hypothesis',
      success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      launch_params: {
        command: 'python train.py',
        declared_metric: 'accuracy',
        metric: 'accuracy',
        cwd: '/workspace',
        env_keys: ['SEED'],
        accelerator: 'T4',
        machine_type: 'n1-standard-1',
        container_image: 'nvcr.io/test:latest',
        provisioning_model: 'SPOT',
        spend_cap_usd: 5.0,
        deadline_seconds: 3600,
        require_live_release: true,
        require_durable_artifact: false,
        arms: null,
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const onCloneExperiment = vi.fn();
    const { findByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" onCloneExperiment={onCloneExperiment} />
    );
    const cloneBtn = await findByTestId('clone-to-form');
    await act(async () => { cloneBtn.click(); });
    expect(onCloneExperiment).toHaveBeenCalledTimes(1);
    const form = onCloneExperiment.mock.calls[0][0];
    expect(form.command).toBe('python train.py');
    expect(form.accelerator).toBe('T4');
    expect(form.machineType).toBe('n1-standard-1');
    expect(form.spendCapUsd).toBe('5');
    expect(form.deadlineSeconds).toBe('3600');
    expect(form.provisioningModel).toBe('SPOT');
    expect(form.envVars).toBe('');
    expect(form.requireLiveRelease).toBe(true);
    expect(form.requireDurableArtifact).toBe(false);
    expect(form.experiment).toBe('My Experiment');
    expect(form.successMetric).toBe('accuracy');
    expect(form.successOperator).toBe('>=');
    expect(form.successThreshold).toBe('0.9');
    expect(form.containerImage).toBe('nvcr.io/test:latest');
    expect(form.useArmsMode).toBe(false);
  });

  test('clone-to-form button not rendered when onCloneExperiment is not provided', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      launch_params: { command: 'python train.py' },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Wait for jobs to load
    await new Promise(r => setTimeout(r, 50));
    expect(queryByTestId('clone-to-form')).toBeNull();
  });

  test('launch action calls onLaunchCompute from the empty compute workbench', async () => {
    const onLaunchCompute = vi.fn();
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" onLaunchCompute={onLaunchCompute} />);
    const launchButton = await findByRole('button', { name: 'Launch experiment' });
    await act(async () => { launchButton.click(); });
    expect(onLaunchCompute).toHaveBeenCalledTimes(1);
  });

  test('run analysis fetches and shows the knowledge doc when an id is returned', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
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

  test('knowledge doc shows acceptance grade badge for compute_experiment', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-badge-1',
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-badge-1',
      title: 'Compute experiment result',
      summary: 'LLM-grounded analysis with artifact citation.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      source_kind: 'compute_experiment',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
      compute_acceptance_grade: 'acceptance',
      compute_acceptance_reason: 'llm_artifact_cited',
    });
    const { findByTestId, getByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-knowledge-doc-link');
    expect(getByText('Acceptance')).toBeTruthy();
  });

  test('knowledge doc shows measured only badge for compute_experiment', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-badge-2',
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-badge-2',
      title: 'Compute experiment result',
      summary: 'Measured-only analysis.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      source_kind: 'compute_experiment',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
      compute_acceptance_grade: 'measured_only',
      compute_acceptance_reason: 'analysis_mode_not_llm',
      analysis_mode: 'deterministic',
    });
    const { findByTestId, getByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-knowledge-doc-link');
    expect(getByText('Measured only')).toBeTruthy();
  });

  test('terminal job with hypothesis and experiment shows experiment identity block', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        experiment: 'sweep-v3',
        hypothesis: 'Increasing learning rate to 0.01 will improve accuracy by 5%.',
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-experiment-identity');
    expect(block.textContent).toContain('sweep-v3');
    expect(block.textContent).toContain('Increasing learning rate to 0.01');
  });

  test('terminal job with benchmark (no experiment) shows Benchmark label', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'FAILED',
        outcome: 'FAILURE',
        final_cost_usd: 0.5,
        benchmark: 'mlperf-v4',
        hypothesis: null,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-experiment-identity');
    expect(block.textContent).toContain('Benchmark');
    expect(block.textContent).toContain('mlperf-v4');
  });

  test('active job shows experiment identity block when fields present', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        experiment: 'sweep-v3',
        hypothesis: 'some hypothesis',
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      }),
    ]));
    const { findByText, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    const identityBlock = await findByTestId('compute-experiment-identity');
    expect(identityBlock.textContent).toContain('sweep-v3');
    expect(identityBlock.textContent).toContain('some hypothesis');
    expect(identityBlock.textContent).toContain('accuracy >= 0.9');
  });

  test('terminal job without experiment/benchmark/hypothesis shows no identity block', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([run1, run2, run3]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([run1, run2]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('experiment-sweep-summary');
    // Best run for latency (lower is better) should be run2 with 80ms
    const bestRow = await findByTestId('sweep-best-run');
    expect(bestRow.textContent).toContain('latency run 2');
    expect(bestRow.textContent).toContain('80');
  });

  test('single terminal job with experiment does not show sweep summary', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        experiment: 'lone-sweep',
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.91, met: true, measurable: true },
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('experiment-sweep-summary')).toBeNull();
  });

  test('running job projected to exceed spend cap before deadline shows a budget-kill warning', async () => {
    // Job started 1 hour ago, has accrued $3 of a $4 cap, deadline is 2h away.
    // Burn rate = $3/hr; projected additional = $3 * 2hr = $6; total projected = $9 > $4 cap.
    const startedAt = new Date(Date.now() - 3600 * 1000).toISOString();
    const deadline = new Date(Date.now() + 2 * 3600 * 1000).toISOString();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        accrued_cost_usd: 3.0,
        spend_cap_usd: 4.0,
        started_at: startedAt,
        wall_clock_deadline: deadline,
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        accrued_cost_usd: 1.0,
        spend_cap_usd: 10.0,
        started_at: startedAt,
        wall_clock_deadline: deadline,
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
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

  test('matrix-verdict-chip renders amber/inconclusive for measurable:false verdict', async () => {
    const jobs = [
      makeJob({
        id: 'job-a',
        task_title: 'run A',
        experiment: 'sweep-inconclusive',
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { result_metrics: { accuracy: 0.9 } },
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: null, met: false, measurable: false },
      }),
      makeJob({
        id: 'job-b',
        task_title: 'run B',
        experiment: 'sweep-inconclusive',
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { result_metrics: { accuracy: 0.95 } },
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
      }),
    ];
    fetchComputeJobs.mockResolvedValue(wrapJobs(jobs));
    const { findByTestId, queryAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    await findByTestId('run-comparison-matrix');
    const chips = queryAllByTestId('matrix-verdict-chip');
    const inconclusiveChip = chips.find((c) => c.textContent?.includes('inconclusive'));
    expect(inconclusiveChip).toBeTruthy();
    expect(inconclusiveChip!.className).toContain('amber');
    expect(inconclusiveChip!.className).not.toContain('red');
  });

  test('sweep run row chip renders amber/inconclusive for measurable:false verdict', async () => {
    const jobs = [
      makeJob({
        id: 'job-s1',
        experiment: 'sweep-exp2',
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { result_metrics: { score: 0.8 } },
        success_criterion: { metric: 'score', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.9, value: null, met: false, measurable: false },
      }),
      makeJob({
        id: 'job-s2',
        experiment: 'sweep-exp2',
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { result_metrics: { score: 0.95 } },
        success_criterion: { metric: 'score', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
      }),
    ];
    fetchComputeJobs.mockResolvedValue(wrapJobs(jobs));
    const { findAllByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const inconclusiveChips = await findAllByText('inconclusive');
    expect(inconclusiveChips.length).toBeGreaterThanOrEqual(1);
    expect(inconclusiveChips[0].className).toContain('amber');
    expect(inconclusiveChips[0].className).not.toContain('red');
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([activeJob, terminalJob]));
    const { findAllByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const titles = await findAllByText('compute_experiment: sweep');
    expect(titles.length).toBeGreaterThanOrEqual(1);
    // Only one terminal job in the group — no summary shown
    expect(queryByTestId('experiment-sweep-summary')).toBeNull();
  });

  test('run analysis without knowledge_document_id still shows feedback without crashing', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    analyzeComputeJob.mockResolvedValue({ status: 'upgraded', knowledge_document_id: null });
    const { findByRole, findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const button = await findByRole('button', { name: 'Run analysis' });
    await act(async () => { button.click(); });
    await findByText('Analysis complete');
    expect(fetchKnowledgeDocument).not.toHaveBeenCalled();
    expect(queryByTestId('compute-analysis-doc')).toBeNull();
  });

  test('terminal job with criterion verdict displays margin above threshold in green', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
      }),
    ]));
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('+0.05');
    expect(margin.classList.contains('text-emerald-300')).toBe(true);
  });

  test('terminal job with criterion verdict displays margin below threshold in red', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.85, met: false, measurable: true },
      }),
    ]));
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('-0.05');
    expect(margin.classList.contains('text-red-300')).toBe(true);
  });

  test('terminal job with <= operator shows correct margin sign', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'latency_ms', operator: '<=', threshold: 100 },
        criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 80, met: true, measurable: true },
      }),
    ]));
    const { findByTestId, getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const margin = getByTestId('criterion-margin');
    expect(margin.textContent).toBe('+20.00');
    expect(margin.classList.contains('text-emerald-300')).toBe(true);
  });

  test('terminal job without criterion_verdict does not show margin', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: null,
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
        criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: null, met: false, measurable: false },
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    expect(queryByTestId('criterion-margin')).toBeNull();
  });

  test('terminal job with release-echo criterion verdict displays binding_status label', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        success_criterion: { metric: 'release_echo_verified', operator: '==', threshold: 1 },
        criterion_verdict: { metric: 'release_echo_verified', operator: '==', threshold: 1, value: 1, met: true, measurable: true, binding_status: 'verified' },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('criterion-verdict-row');
    const bindingStatus = await findByTestId('criterion-binding-status');
    expect(bindingStatus.textContent).toBe('verified');
  });

  test('window selector renders with 6h active by default', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const selector = await findByTestId('compute-window-selector');
    expect(selector.textContent).toContain('6h');
    expect(selector.textContent).toContain('24h');
    expect(selector.textContent).toContain('3d');
    expect(selector.textContent).toContain('7d');
    // Initially fetched with default (no window arg or default 360)
    expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', expect.anything(), { includeFinishedMinutes: 360, offset: 0 });
  });

  test('clicking 7d window selector triggers refetch with 10080 minutes', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-window-selector');
    const btn7d = await findByTestId('compute-window-7d');
    await act(async () => { btn7d.click(); });
    await waitFor(() =>
      expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', expect.anything(), { includeFinishedMinutes: 10080, offset: 0 }),
    );
  });

  test('compute panel summary renders the outcome breakdown counts', async () => {
    fetchComputeJobs.mockResolvedValue({
      jobs: [
        makeJob({ id: 'job-a', status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
        makeJob({ id: 'job-b', status: 'FAILED', outcome: 'FAILURE', final_cost_usd: 0.5 }),
      ],
      hasMore: false,
      summary: {
        total_jobs: 4,
        active_jobs: 0,
        terminal_jobs: 4,
        total_spend_usd: 3.0,
        outcome_breakdown: {
          success: 2,
          failure: 1,
          cancelled: 1,
        },
        criterion_measured: 0,
        criterion_met: 0,
      },
      limit: 50,
      offset: 0,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const summary = await findByTestId('compute-jobs-summary');
    // Outcome breakdown section should be visible with header
    expect(summary.textContent).toContain('Outcomes');
    // All outcome counts should be displayed in the summary
    expect(summary.textContent).toContain('success');
    expect(summary.textContent).toContain('2');
    expect(summary.textContent).toContain('failure');
    expect(summary.textContent).toContain('1');
    expect(summary.textContent).toContain('cancelled');
  });

  test('command log with more than 30 lines shows Show all button; clicking expands', async () => {
    const longLog = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n');
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { command_log: { text: longLog, truncated: false, byte_count: longLog.length } },
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        polling_metadata: { command_log: { text: shortLog, truncated: false, byte_count: shortLog.length } },
      }),
    ]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
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

  test('arms breakdown renders a failed arm with exit code chip and error excerpt in red styling', async () => {
    const armsJob = makeJob({
      id: 'job-failed-arm',
      task_title: 'failed arm experiment',
      status: 'FAILED',
      outcome: 'FAILURE',
      final_cost_usd: 1.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-ok': { score: 0.85 },
            'arm-crash': { exit_code: 1, error: 'OOM at step 40', stderr_excerpt: 'RuntimeError: CUDA OOM' },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const armsBlock = await findByTestId('arms-breakdown');
    // Failed arm shows exit code chip
    const exitChips = getAllByTestId('arm-exit-code');
    expect(exitChips).toHaveLength(1);
    expect(exitChips[0].textContent).toBe('exit 1');
    // Failed arm shows error excerpt
    const errorBlocks = getAllByTestId('arm-error');
    expect(errorBlocks).toHaveLength(1);
    expect(errorBlocks[0].textContent).toContain('OOM at step 40');
    // stderr excerpt shown
    const stderrBlocks = getAllByTestId('arm-stderr');
    expect(stderrBlocks).toHaveLength(1);
    expect(stderrBlocks[0].textContent).toContain('CUDA OOM');
    // Failed arm card uses red border/background classes
    const armCards = armsBlock.querySelectorAll('[class*="border-red"]');
    expect(armCards.length).toBeGreaterThan(0);
    // Diagnostic keys do NOT appear as plain metric rows
    expect(armsBlock.textContent).not.toContain('exit_code:');
    expect(armsBlock.textContent).not.toContain('stderr_excerpt:');
    // Successful arm does not have red border
    expect(armsBlock.textContent).toContain('arm-ok');
  });

  test('arm with reason field (no exit_code) renders error in arm-error', async () => {
    const armsJob = makeJob({
      id: 'job-arms-reason',
      task_title: 'reason experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-timeout': { reason: 'arm result file not written', accuracy: 0.0 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('arms-breakdown');
    const errors = getAllByTestId('arm-error');
    expect(errors[0].textContent).toContain('arm result file not written');
    // reason must NOT appear as a plain string metric row
    const armsBlock = await findByTestId('arms-breakdown');
    expect(armsBlock.textContent).not.toContain('reason:');
  });

  test('arms breakdown does not apply failure styling when exit_code is 0', async () => {
    const armsJob = makeJob({
      id: 'job-success-arm',
      task_title: 'success arm experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-a': { exit_code: 0, score: 0.92 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, queryAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const armsBlock = await findByTestId('arms-breakdown');
    // Exit code chip shown (exit 0)
    const exitChips = queryAllByTestId('arm-exit-code');
    expect(exitChips).toHaveLength(1);
    expect(exitChips[0].textContent).toBe('exit 0');
    // No red styling — success exit
    const armCards = armsBlock.querySelectorAll('[class*="border-red"]');
    expect(armCards.length).toBe(0);
    // No error block
    expect(queryAllByTestId('arm-error')).toHaveLength(0);
    // exit_code NOT shown as a plain metric row
    expect(armsBlock.textContent).not.toContain('exit_code:');
  });

  test('run comparison matrix arms-comparison section tints a failed arm name in red', async () => {
    const job1 = makeJob({
      id: 'arms-fail-matrix-1',
      task_title: 'matrix run A',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'fail-matrix-test',
      criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.8, value: 0.85, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          score: 0.85,
          arms: {
            'arm-good': { score: 0.85 },
            'arm-bad': { exit_code: 2, error: 'segfault in eval loop' },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'arms-fail-matrix-2',
      task_title: 'matrix run B',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.2,
      experiment: 'fail-matrix-test',
      criterion_verdict: { metric: 'score', operator: '>=', threshold: 0.8, value: 0.79, met: false, measurable: true },
      polling_metadata: { result_metrics: { score: 0.79 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const armsSection = await findByTestId('arms-comparison');
    // Error excerpt is visible in the comparison section
    expect(armsSection.textContent).toContain('arm-bad');
    expect(armsSection.textContent).toContain('segfault in eval loop');
    // Failed arm name is rendered with red styling
    const redArmNames = armsSection.querySelectorAll('[class*="text-red"]');
    expect(redArmNames.length).toBeGreaterThan(0);
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
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
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const armsSection = await findByTestId('arms-comparison');
    expect(armsSection.textContent).toContain('baseline');
    expect(armsSection.textContent).toContain('experimental');
    expect(armsSection.textContent).toContain('auc:');
    expect(armsSection.textContent).toContain('0.85');
  });

  test('clicking All window button calls fetchComputeJobsPaged with allTerminal=true and clears window highlight', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const allBtn = await findByTestId('compute-window-all');
    await act(async () => { allBtn.click(); });
    await waitFor(() =>
      expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', expect.anything(), { allTerminal: true, offset: 0 }),
    );
    // The All button should now be active (highlighted)
    expect(allBtn.className).toContain('bg-neutral-700');
    // The 6h button should not be active
    const btn6h = await findByTestId('compute-window-6h');
    expect(btn6h.className).not.toContain('bg-neutral-700');
  });

  test('Load more button appears when hasMore is true and clicking appends jobs', async () => {
    const job1 = makeJob({ id: 'job-1', task_title: 'Job One' });
    const job2 = makeJob({ id: 'job-2', task_title: 'Job Two', status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs
      .mockResolvedValueOnce(wrapJobs([]))  // initial render (window mode)
      .mockResolvedValueOnce({ jobs: [job1], hasMore: true })  // after clicking All
      .mockResolvedValueOnce({ jobs: [job2], hasMore: false });  // after Load more
    const { findByTestId, findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const allBtn = await findByTestId('compute-window-all');
    await act(async () => { allBtn.click(); });
    const loadMoreBtn = await findByTestId('compute-load-more');
    expect(loadMoreBtn).toBeTruthy();
    await act(async () => { loadMoreBtn.click(); });
    await waitFor(() => expect(queryByTestId('compute-load-more')).toBeNull());
    expect(await findByText('Job One')).toBeTruthy();
    expect(await findByText('Job Two')).toBeTruthy();
  });

  test('Load more in window mode passes offset so second page is appended without duplicates', async () => {
    const job1 = makeJob({ id: 'win-job-1', task_title: 'Window Job One' });
    const job2 = makeJob({ id: 'win-job-2', task_title: 'Window Job Two', status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs
      .mockResolvedValueOnce({ jobs: [job1], hasMore: true })  // initial window-mode load
      .mockResolvedValueOnce({ jobs: [job2], hasMore: false }); // Load more with offset=1
    const { findByTestId, findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const loadMoreBtn = await findByTestId('compute-load-more');
    expect(loadMoreBtn).toBeTruthy();
    await act(async () => { loadMoreBtn.click(); });
    await waitFor(() => expect(queryByTestId('compute-load-more')).toBeNull());
    // Both jobs must be visible (not duplicated)
    expect(await findByText('Window Job One')).toBeTruthy();
    expect(await findByText('Window Job Two')).toBeTruthy();
    // Second call must include offset=1 to avoid page 0 duplication
    expect(fetchComputeJobs).toHaveBeenNthCalledWith(2, 'proj-1', undefined, { includeFinishedMinutes: 360, offset: 1 });
  });

  test('job with external_compute_id shows it in the machine info subtitle', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ external_compute_id: 'compute-job-abc-123' }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const subtitle = await findByTestId('compute-job-provenance');
    expect(subtitle.textContent).toContain('gcp_compute_engine');
    expect(subtitle.textContent).toContain('compute-job-abc-123');
  });

  test('shows job provenance identity', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        backend: 'gcp_compute_engine',
        machine_type: 'e2-standard-2',
        external_compute_id: 'compute-job-vm-1',
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // The provenance line is the machine-testable seam that identifies WHERE a
    // compute result was produced: backend, machine type, and the cloud VM identity.
    const provenance = await findByTestId('compute-job-provenance');
    expect(provenance.textContent).toContain('gcp_compute_engine');
    expect(provenance.textContent).toContain('e2-standard-2');
    // The cloud VM identity is explicitly labelled so provenance reads as an
    // identity rather than an anonymous suffix.
    const vmIdentity = await findByTestId('compute-job-vm-identity');
    expect(vmIdentity.textContent).toBe('VM compute-job-vm-1');
    expect(provenance.contains(vmIdentity)).toBe(true);
  });

  test('renders aggregate summary header when summary has jobs', async () => {
    const job = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 2.5 });
    fetchComputeJobs.mockResolvedValue({
      jobs: [job],
      hasMore: false,
      summary: {
        total_jobs: 5,
        active_jobs: 1,
        terminal_jobs: 4,
        total_spend_usd: 7.25,
        outcome_breakdown: { SUCCESS: 3, FAILURE: 1 },
        criterion_measured: 2,
        criterion_met: 1,
      },
      limit: 50,
      offset: 0,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const summary = await findByTestId('compute-jobs-summary');
    expect(summary.textContent).toContain('5');   // total
    expect(summary.textContent).toContain('1');   // active
    expect(summary.textContent).toContain('$7.25'); // spend
    expect(summary.textContent).toContain('1/2'); // criterion met/measured
  });

  test('does not render summary header when total_jobs is 0', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByTestId('compute-jobs-summary')).toBeNull();
  });

  test('shows Load-more button when has_more is true and hides it when false', async () => {
    fetchComputeJobs.mockResolvedValueOnce({
      jobs: [makeJob()],
      hasMore: true,
      summary: { total_jobs: 3, active_jobs: 1, terminal_jobs: 2, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 1,
      offset: 0,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const btn = await findByTestId('compute-load-more');
    expect(btn.textContent).toBe('Load more');
  });

  test('Load-more fetches next page and appends jobs without replacing existing ones', async () => {
    const page1Job = makeJob({ id: 'job-p1', task_title: 'page 1 job' });
    const page2Job = makeJob({ id: 'job-p2', task_title: 'page 2 job', status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs
      .mockResolvedValueOnce({
        jobs: [page1Job],
        hasMore: true,
        summary: { total_jobs: 2, active_jobs: 1, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
        limit: 1,
        offset: 0,
      })
      .mockResolvedValueOnce({
        jobs: [page2Job],
        hasMore: false,
        summary: { total_jobs: 2, active_jobs: 1, terminal_jobs: 1, total_spend_usd: 1.0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
        limit: 1,
        offset: 1,
      });
    const { findByTestId, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    // First page loaded
    await findByText('page 1 job');
    // Load more
    const loadMoreBtn = await findByTestId('compute-load-more');
    await act(async () => { loadMoreBtn.click(); });
    // Both pages now visible
    await findByText('page 1 job');
    await findByText('page 2 job');
    // Load-more button gone after last page
    await waitFor(() => expect(document.querySelector('[data-testid="compute-load-more"]')).toBeNull());
    // fetchComputeJobs called with offset=1 for the second page
    expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', undefined, { includeFinishedMinutes: 360, offset: 1 });
  });

  test('renders lifetime aggregate summary via fetchComputeJobsAggregateSummary', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 2.5,
      }),
    ]));
    fetchComputeJobsAggregateSummary.mockResolvedValue({
      total_jobs: 150,
      active_jobs: 0,
      terminal_jobs: 150,
      total_spend_usd: 87.50,
      outcome_breakdown: { SUCCESS: 120, FAILURE: 30 },
      criterion_measured: 100,
      criterion_met: 85,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const lifetime = await findByTestId('compute-lifetime-summary');
    expect(lifetime.textContent).toContain('Lifetime');
    expect(lifetime.textContent).toContain('150');
    expect(lifetime.textContent).toContain('$87.50');
    expect(lifetime.textContent).toContain('85/100');
    // Verify lifetime summary is fetched for the project
    expect(fetchComputeJobsAggregateSummary).toHaveBeenCalledWith('proj-1');
  });

  test('lifetime summary and windowed summary are distinct', async () => {
    const windowedJob = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.5,
    });
    fetchComputeJobs.mockResolvedValue({
      jobs: [windowedJob],
      hasMore: false,
      summary: {
        total_jobs: 5,
        active_jobs: 0,
        terminal_jobs: 5,
        total_spend_usd: 12.50,
        outcome_breakdown: { SUCCESS: 4, FAILURE: 1 },
        criterion_measured: 3,
        criterion_met: 2,
      },
      limit: 50,
      offset: 0,
    });
    fetchComputeJobsAggregateSummary.mockResolvedValue({
      total_jobs: 150,
      active_jobs: 0,
      terminal_jobs: 150,
      total_spend_usd: 87.50,
      outcome_breakdown: { SUCCESS: 120, FAILURE: 30 },
      criterion_measured: 100,
      criterion_met: 85,
    });
    const { findByTestId, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Both summaries should render
    await findByTestId('compute-lifetime-summary');
    await findByTestId('compute-jobs-summary');
    // Lifetime totals: 150 total, $87.50 spend
    const lifetime = container.querySelector('[data-testid="compute-lifetime-summary"]');
    expect(lifetime?.textContent).toContain('150');
    expect(lifetime?.textContent).toContain('$87.50');
    // Windowed totals: 5 total, $12.50 spend
    const windowed = container.querySelector('[data-testid="compute-jobs-summary"]');
    expect(windowed?.textContent).toContain('5');
    expect(windowed?.textContent).toContain('$12.50');
    // Labels must be present to distinguish them
    expect(lifetime?.textContent).toContain('Lifetime');
    expect(windowed?.textContent).toContain('Windowed');
  });

  test('lifetime summary gracefully handles fetch errors', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchComputeJobsAggregateSummary.mockRejectedValue(new Error('network error'));
    const { findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Component still renders without crashing despite the fetch error
    await findByText('No compute jobs');
    // Lifetime summary block is absent (no data)
    expect(document.querySelector('[data-testid="compute-lifetime-summary"]')).toBeNull();
  });

  test('lifetime summary does not render when total_jobs is 0', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchComputeJobsAggregateSummary.mockResolvedValue({
      total_jobs: 0,
      active_jobs: 0,
      terminal_jobs: 0,
      total_spend_usd: 0,
      outcome_breakdown: {},
      criterion_measured: 0,
      criterion_met: 0,
    });
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByTestId('compute-lifetime-summary')).toBeNull();
  });

  test('renders held experiments section when tasks with requires_approval exist', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchHeldComputeExperiments.mockResolvedValue([
      {
        id: 'task-held-1',
        title: 'sweep: test convergence',
        status: 'QUEUED',
        requires_approval: true,
        payloads: [{ type: 'compute_experiment', spend_cap_usd: 12.5 }],
        created_at: new Date().toISOString(),
      },
    ]);
    const { findByTestId, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('held-experiments-section');
    expect(await findByText(/sweep: test convergence/)).toBeTruthy();
    expect(await findByText(/\$12\.50/)).toBeTruthy();
  });

  test('approve button calls onApproveExperiment with task id', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchHeldComputeExperiments.mockResolvedValue([
      {
        id: 'task-held-2',
        title: 'held experiment',
        status: 'QUEUED',
        requires_approval: true,
        payloads: [{ type: 'compute_experiment', spend_cap_usd: 5.0 }],
        created_at: new Date().toISOString(),
      },
    ]);
    const onApprove = vi.fn().mockResolvedValue(undefined);
    const { findByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" onApproveExperiment={onApprove} />,
    );
    const btn = await findByTestId('held-experiment-approve');
    btn.click();
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith('task-held-2'));
  });

  test('dismiss button calls onDismissExperiment with task id', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchHeldComputeExperiments.mockResolvedValue([
      {
        id: 'task-held-3',
        title: 'held experiment',
        status: 'QUEUED',
        requires_approval: true,
        payloads: [{ type: 'compute_experiment', spend_cap_usd: 5.0 }],
        created_at: new Date().toISOString(),
      },
    ]);
    const onDismiss = vi.fn().mockResolvedValue(undefined);
    const { findByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" onDismissExperiment={onDismiss} />,
    );
    const btn = await findByTestId('held-experiment-dismiss');
    btn.click();
    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith('task-held-3'));
  });

  test('held experiments section is absent when no held tasks exist', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchHeldComputeExperiments.mockResolvedValue([]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByTestId('held-experiments-section')).toBeNull();
  });

  test('renders awaiting-local-runner section with title, spend cap, and waiting indicator', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    // Held ~5 minutes ago so the duration formats to a stable "5m" scale
    // regardless of the second-or-two of test drift.
    const since = new Date(Date.now() - 300_000).toISOString();
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([
      {
        id: 'task-await-1',
        title: 'local sweep: qlora',
        awaiting_local_runner_since: since,
        created_at: since,
        requires_approval: false,
        spend_cap_usd: 8.5,
        command: 'python train.py',
        machine_type: null,
        accelerator: null,
        container_image: null,
        provisioning_model: null,
        deadline_seconds: null,
      },
    ]);
    const { findByTestId, findByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    const section = await findByTestId('awaiting-local-runner-section');
    expect(section.textContent).toContain('local sweep: qlora');
    expect(await findByText(/\$8\.50/)).toBeTruthy();
    const waiting = await findByTestId('awaiting-local-runner-waiting');
    expect(waiting.textContent).toContain('waiting for local runner');
    expect(waiting.textContent).toMatch(/since .*ago/);
    expect(waiting.textContent).toContain('5m');
  });

  test('awaiting-local-runner section is absent when no holds exist', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByTestId('awaiting-local-runner-section')).toBeNull();
  });

  test('awaiting-local-runner waiting indicator advances as the 1s clock ticks', async () => {
    vi.useFakeTimers();
    try {
      const base = new Date('2026-07-04T00:00:00.000Z').getTime();
      vi.setSystemTime(base);
      fetchComputeJobs.mockResolvedValue(wrapJobs([]));
      fetchAwaitingLocalRunnerHolds.mockResolvedValue([
        {
          id: 'task-await-2',
          title: 'local sweep: clock',
          awaiting_local_runner_since: new Date(base - 10_000).toISOString(),
          created_at: new Date(base - 10_000).toISOString(),
          requires_approval: false,
          spend_cap_usd: null,
          command: null,
          machine_type: null,
          accelerator: null,
          container_image: null,
          provisioning_model: null,
          deadline_seconds: null,
        },
      ]);
      const { getByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
      // Flush the leading poller promise so the hold state is applied.
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(getByTestId('awaiting-local-runner-waiting').textContent).toContain('10s');
      // The local 1s clock should advance the live duration.
      await act(async () => { vi.advanceTimersByTime(5_000); });
      expect(getByTestId('awaiting-local-runner-waiting').textContent).toContain('15s');
    } finally {
      vi.useRealTimers();
    }
  });

  test('renders View in Library button when knowledge doc present and onNavigateToKnowledge is provided, calls it with doc id', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-lib-1',
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-lib-1',
      title: 'Experiment: accuracy result',
      summary: 'Achieved 91% accuracy.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const onNavigateToKnowledge = vi.fn();
    const { findByTestId } = render(
      <ComputeJobsPanel projectId="proj-1" onNavigateToKnowledge={onNavigateToKnowledge} />,
    );
    const libraryBtn = await findByTestId('compute-knowledge-doc-library-link');
    expect(libraryBtn).toBeTruthy();
    await act(async () => { libraryBtn.click(); });
    expect(onNavigateToKnowledge).toHaveBeenCalledWith('doc-lib-1');
  });

  test('does not render View in Library button when onNavigateToKnowledge is not provided', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      knowledge_document_id: 'doc-lib-2',
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    fetchKnowledgeDocument.mockResolvedValue({
      id: 'doc-lib-2',
      title: 'Experiment: no library link',
      summary: 'Some result.',
      tags: [],
      source_citations: [],
      source_task_id: 'task-1',
      created_at: null,
      updated_at: null,
      forward_quality_verdict: null,
    });
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Verify the doc card renders (so we know the test setup is correct)
    await findByTestId('compute-knowledge-doc-link');
    expect(queryByTestId('compute-knowledge-doc-library-link')).toBeNull();
  });

  test('arms breakdown renders winner verdict and positive margin for higher_better criterion metric', async () => {
    const armsJob = makeJob({
      id: 'job-verdict',
      task_title: 'verdict experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      success_criterion: { metric: 'val_acc', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'val_acc', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-a': { val_acc: 0.90, lr: 0.01 },
            'arm-b': { val_acc: 0.95, lr: 0.001 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const verdict = await findByTestId('arm-comparison-verdict');
    expect(verdict.textContent).toContain('Winner');
    expect(verdict.textContent).toContain('arm-b');
    const marginChip = await findByTestId('arm-comparison-margin');
    // arm-b (0.95) beats arm-a (0.90) by 0.05
    expect(marginChip.textContent).toContain('+0.05');
    expect(marginChip.textContent).toContain('arm-a');
    expect(marginChip.classList.contains('text-emerald-300')).toBe(true);
  });

  test('arms breakdown renders no verdict when fewer than two arms have the criterion metric value', async () => {
    const armsJob = makeJob({
      id: 'job-no-verdict',
      task_title: 'incomplete arms experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      success_criterion: { metric: 'val_acc', operator: '>=', threshold: 0.9 },
      criterion_verdict: { metric: 'val_acc', operator: '>=', threshold: 0.9, value: 0.91, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-a': { val_acc: 0.91 },
            'arm-b': { exit_code: 1, error: 'OOM' },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Arms breakdown renders but no verdict since only arm-a has val_acc
    await findByTestId('arms-breakdown');
    expect(queryByTestId('arm-comparison-verdict')).toBeNull();
  });

  test('operator-hardware job (local-subprocess) shows friendly label and $0.00 not-billed cost', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ backend: 'local-subprocess', spend_cap_usd: 4.0, accrued_cost_usd: 0 }),
    ]));
    const { findByText, findByTestId, queryByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    // Friendly label in subtitle
    const subtitle = await findByText((t) => t.includes('Operator hardware'));
    expect(subtitle.textContent).toContain('Operator hardware');
    expect(subtitle.textContent).not.toContain('local-subprocess');
    // Cost block shows $0.00 + not-billed label instead of budget bar
    const costRow = await findByTestId('compute-operator-cost');
    expect(costRow.textContent).toContain('$0.00');
    expect(costRow.textContent).toContain('not billed at cloud rates');
    // No budget bar readout
    expect(queryByText(/Budget/)).toBeNull();
  });

  test('operator-hardware GPU job (local-gpu) shows GPU label and $0.00 not-billed cost', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ backend: 'local-gpu', spend_cap_usd: 4.0, accrued_cost_usd: 0 }),
    ]));
    const { findByText, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    const subtitle = await findByText((t) => t.includes('Operator hardware · GPU'));
    expect(subtitle.textContent).toContain('Operator hardware · GPU');
    const costRow = await findByTestId('compute-operator-cost');
    expect(costRow.textContent).toContain('$0.00');
    expect(costRow.textContent).toContain('not billed at cloud rates');
  });

  test('GCP job still shows budget bar with real cost amounts (non-regression)', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ backend: 'gcp_compute_engine', accrued_cost_usd: 2.0, spend_cap_usd: 4.0 }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('$2.00 / $4.00');
    // Standard budget bar rendered, not the operator cost row
    expect(queryByTestId('compute-operator-cost')).toBeNull();
  });

  test('provisioning-death path is unaffected when backend is gcp (non-regression)', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        backend: 'gcp_compute_engine',
        status: 'TIMED_OUT',
        outcome: 'TIMED_OUT',
        final_cost_usd: 0,
        terminal_reason: 'compute job timed out while provisioning',
        vm_phase: 'provisioning',
        launch_contract: { requested_deadline_seconds: 600, provisioning_model: 'best_effort' },
        compute_rpc_diagnostic: makeDiagnostic({
          operation: 'launch',
          error_type: 'QuotaExceeded',
          error_message: 'quota exceeded',
        }),
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Provisioning cause block shows for gcp
    await findByTestId('compute-provisioning-cause');
    // No operator-cost row for a gcp backend
    expect(queryByTestId('compute-operator-cost')).toBeNull();
  });

  test('arms breakdown verdict shows correct winner for lower_better (<=) criterion metric', async () => {
    const armsJob = makeJob({
      id: 'job-lower-better',
      task_title: 'latency experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      success_criterion: { metric: 'latency_ms', operator: '<=', threshold: 100 },
      criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 80, met: true, measurable: true },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-fast': { latency_ms: 80 },
            'arm-slow': { latency_ms: 120 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const verdict = await findByTestId('arm-comparison-verdict');
    // arm-fast wins (lower latency is better)
    expect(verdict.textContent).toContain('arm-fast');
    const marginChip = await findByTestId('arm-comparison-margin');
    // margin = 120 - 80 = 40
    expect(marginChip.textContent).toContain('+40.00');
    expect(marginChip.textContent).toContain('arm-slow');
  });

  test('compares_runs_grouped_by_sweep_id renders backend criterion verdict values', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchComputeSweepGroups.mockResolvedValue({
      sweep_groups: {
        'sweep-abc': [
          { id: 'doc-1', title: 'accuracy_v1 sweep run A', summary: 'Achieved 0.91 accuracy', compute_acceptance_grade: 'measured_only', criterion_met: false, criterion: { metric: 'accuracy', operator: '>=', threshold: 0.93 }, per_arm_verdicts: { 'run-a': { value: 0.91, met: false, cost_usd: 0.42 } }, arm_label: 'run-a', created_at: '2026-06-20T10:00:00Z' },
          { id: 'doc-2', title: 'accuracy_v1 sweep run B', summary: 'Achieved 0.95 accuracy', compute_acceptance_grade: 'acceptance', criterion_met: true, criterion: { metric: 'accuracy', operator: '>=', threshold: 0.93 }, per_arm_verdicts: { 'run-b': { value: 0.95, met: true, cost_usd: 0.53 } }, arm_label: 'run-b', created_at: '2026-06-20T11:00:00Z' },
        ],
      },
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // The sweep knowledge groups section renders
    await findByTestId('sweep-knowledge-groups');
    // The first doc's title appears as the group label
    const groupLabel = await findByTestId('sweep-knowledge-group-label');
    expect(groupLabel.textContent).toContain('accuracy_v1 sweep run A');
    // Toggle the compare view
    const compareBtn = await findByTestId('sweep-knowledge-compare-toggle');
    await act(async () => { compareBtn.click(); });
    // Comparison table rendered
    const table = await findByTestId('sweep-knowledge-comparison-table');
    expect(table.textContent).toContain('run-a');
    expect(table.textContent).toContain('run-b');
    expect(table.textContent).toContain('NOT MET / measured');
    expect(table.textContent).toContain('MET / accepted');
    expect(table.textContent).toContain('accuracy >= 0.93');
    expect(table.textContent).toContain('run-b: 0.95 MET ($0.53)');
  });

  test('compares_runs_grouped_by_sweep_id: hides sweep knowledge groups section when no groups returned', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    fetchComputeSweepGroups.mockResolvedValue({ sweep_groups: {} });
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('No compute jobs');
    expect(queryByTestId('sweep-knowledge-groups')).toBeNull();
  });

  test('disables re-run button when job has no source task', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0, task_id: null });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findByRole } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByRole('button', { name: 'Run analysis' });
    const rerunBtn = await findByRole('button', { name: 'Re-run' });
    expect(rerunBtn).toBeDisabled();
  });

  test('re-run button opens inline panel with pre-populated fields', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0, task_id: 'task-1',
      spend_cap_usd: 4.0,
      launch_contract: { requested_deadline_seconds: 900 },
      success_criterion: { metric: 'tokens_per_second', operator: '>=' as const, threshold: 100 },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findByRole, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByRole('button', { name: 'Run analysis' });
    const rerunBtn = await findByRole('button', { name: 'Re-run' });
    expect(rerunBtn).not.toBeDisabled();
    await act(async () => { rerunBtn.click(); });
    const panel = await findByTestId('rerun-inline-panel');
    expect(panel).toBeTruthy();
    const spendInput = await findByTestId('rerun-spend-cap') as HTMLInputElement;
    expect(spendInput.value).toBe('4');
    const deadlineInput = await findByTestId('rerun-deadline-sec') as HTMLInputElement;
    expect(deadlineInput.value).toBe('900');
    const thresholdInput = await findByTestId('rerun-threshold') as HTMLInputElement;
    expect(thresholdInput.value).toBe('100');
  });

  test('inline rerun panel calls rerunComputeExperiment on submit', async () => {
    rerunComputeExperiment.mockResolvedValue({ id: 'new-task-1' });
    fetchComputeJobs.mockResolvedValue(wrapJobs([]));
    const terminal = makeJob({
      status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0, task_id: 'task-1',
      spend_cap_usd: 4.0,
      success_criterion: { metric: 'acc', operator: '>=' as const, threshold: 0.9 },
    });
    fetchComputeJobs.mockResolvedValueOnce(wrapJobs([terminal]));
    const { findByRole, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByRole('button', { name: 'Run analysis' });
    await act(async () => { (await findByRole('button', { name: 'Re-run' })).click(); });
    await findByTestId('rerun-inline-panel');
    await act(async () => { (await findByTestId('rerun-submit')).click(); });
    expect(rerunComputeExperiment).toHaveBeenCalledWith('task-1', expect.objectContaining({ spend_cap_usd: 4.0 }));
  });

  test('surfaces compute re-run response and held approval state', async () => {
    rerunComputeExperiment.mockResolvedValue({ id: 'new-task-xyz', status: 'QUEUED', requires_approval: true });
    const terminal = makeJob({
      status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0, task_id: 'task-1',
      spend_cap_usd: 4.0,
      success_criterion: { metric: 'acc', operator: '>=' as const, threshold: 0.9 },
    });
    // Set default mock to return the terminal job for all subsequent load() calls after the initial render
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    // First call during render will use this override
    fetchComputeJobs.mockResolvedValueOnce(wrapJobs([terminal]));
    const { findByRole, findByTestId, getByText } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByRole('button', { name: 'Run analysis' });
    await act(async () => { (await findByRole('button', { name: 'Re-run' })).click(); });
    await findByTestId('rerun-inline-panel');
    await act(async () => { (await findByTestId('rerun-submit')).click(); });
    expect(rerunComputeExperiment).toHaveBeenCalledWith('task-1', expect.objectContaining({ spend_cap_usd: 4.0 }));
    // Verify the new task id is displayed
    await findByTestId('rerun-inline-panel');
    expect(getByText('new-task-xyz')).toBeTruthy();
    // Verify the approval requirement is displayed
    expect(getByText('Held for approval')).toBeTruthy();
  });

  test('arms breakdown shows per-arm pass/fail chip and cost when per_arm_verdicts present', async () => {
    const armsJob = makeJob({
      id: 'job-pav',
      task_title: 'DEN vs NN experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'DEN': { score: 0.91 },
            'NN': { score: 0.85 },
          },
        },
      },
      per_arm_verdicts: {
        'DEN': { value: 0.91, met: true, cost_usd: 1.20 },
        'NN': { value: 0.85, met: false, cost_usd: 0.80 },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const armsBlock = await findByTestId('arms-breakdown');
    // Both arm names present
    expect(armsBlock.textContent).toContain('DEN');
    expect(armsBlock.textContent).toContain('NN');
    // pass/fail chips appear for both arms
    const verdictChips = getAllByTestId('arm-verdict-chip');
    expect(verdictChips).toHaveLength(2);
    const passChip = verdictChips.find((c) => c.textContent === 'pass');
    const failChip = verdictChips.find((c) => c.textContent === 'fail');
    expect(passChip).toBeTruthy();
    expect(failChip).toBeTruthy();
    expect(passChip!.className).toContain('emerald');
    expect(failChip!.className).toContain('red');
    // cost chips appear for both arms
    const costChips = getAllByTestId('arm-cost');
    expect(costChips).toHaveLength(2);
    expect(armsBlock.textContent).toContain('$1.20');
    expect(armsBlock.textContent).toContain('$0.80');
  });

  test('arms breakdown shows amber n/a chip when per_arm_verdicts has met=null (unmeasured arm)', async () => {
    const armsJob = makeJob({
      id: 'job-pav-null-met',
      task_title: 'null met experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-measured': { score: 0.92 },
            'arm-crashed': {},
          },
        },
      },
      per_arm_verdicts: {
        'arm-measured': { value: 0.92, met: true, cost_usd: 1.00 },
        'arm-crashed': { value: null, met: null, cost_usd: 0.50 },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('arms-breakdown');
    const verdictChips = getAllByTestId('arm-verdict-chip');
    expect(verdictChips).toHaveLength(2);
    const passChip = verdictChips.find((c) => c.textContent === 'pass');
    const naChip = verdictChips.find((c) => c.textContent === 'n/a');
    expect(passChip).toBeTruthy();
    expect(naChip).toBeTruthy();
    expect(passChip!.className).toContain('emerald');
    expect(naChip!.className).toContain('amber');
  });

  test('arms breakdown omits verdict chip and cost when per_arm_verdicts absent', async () => {
    const armsJob = makeJob({
      id: 'job-no-pav',
      task_title: 'no verdict experiment',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-a': { score: 0.9 },
            'arm-b': { score: 0.8 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([armsJob]));
    const { findByTestId, queryAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('arms-breakdown');
    expect(queryAllByTestId('arm-verdict-chip')).toHaveLength(0);
    expect(queryAllByTestId('arm-cost')).toHaveLength(0);
  });

  test('test_per_arm_comparison_renders_cost_and_passfail_chip', async () => {
    // job1 has per-arm costs and criterion_verdict.per_arm so the comparison
    // matrix can show numeric cost_usd chips and pass/fail verdicts per arm.
    const job1 = makeJob({
      id: 'perarm-job1',
      task_title: 'run A',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      experiment: 'perarm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.95,
        met: true,
        measurable: true,
        per_arm: { 'arm-good': 0.95, 'arm-bad': 0.82 },
      },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-good': { accuracy: 0.95, cost_usd: 1.50, elapsed_seconds: 1800 },
            'arm-bad': { accuracy: 0.82, cost_usd: 0.50, elapsed_seconds: 600 },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'perarm-job2',
      task_title: 'run B',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      experiment: 'perarm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.88,
        met: false,
        measurable: true,
      },
      polling_metadata: { result_metrics: { accuracy: 0.88 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Open comparison mode
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    // arms-comparison section appears (job1 has arms)
    const armsSection = await findByTestId('arms-comparison');
    expect(armsSection).toBeTruthy();
    // Per-arm costs rendered as formatted USD chips
    const costChips = getAllByTestId('arm-cost-usd');
    expect(costChips.length).toBeGreaterThanOrEqual(2);
    const costTexts = costChips.map((c) => c.textContent);
    expect(costTexts.some((t) => t?.includes('$1.50'))).toBe(true);
    expect(costTexts.some((t) => t?.includes('$0.50'))).toBe(true);
    // cost_usd must not also appear as plain "cost_usd: 1.5" metric text
    expect(armsSection.textContent).not.toContain('cost_usd:');
    // Per-arm pass/fail chips: arm-good passes (0.95 >= 0.9), arm-bad fails (0.82 < 0.9)
    const pfChips = getAllByTestId('arm-passfail-chip');
    const pfTexts = pfChips.map((c) => c.textContent);
    expect(pfTexts).toContain('pass');
    expect(pfTexts).toContain('fail');
    // pass chip uses emerald styling, fail chip uses red
    const passChip = pfChips.find((c) => c.textContent === 'pass');
    const failChip = pfChips.find((c) => c.textContent === 'fail');
    expect(passChip?.className).toContain('emerald');
    expect(failChip?.className).toContain('red');
  });

  test('arms comparison section shows N-of-M arms met criterion rollup count', async () => {
    const job1 = makeJob({
      id: 'arms-rollup-1',
      task_title: 'arms rollup run',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 2.0,
      experiment: 'arms-rollup-test',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.92,
        met: true,
        measurable: true,
        per_arm: { 'arm-alpha': 0.95, 'arm-beta': 0.82, 'arm-gamma': 0.93 },
      },
      polling_metadata: {
        result_metrics: {
          accuracy: 0.92,
          arms: {
            'arm-alpha': { accuracy: 0.95 },
            'arm-beta': { accuracy: 0.82 },
            'arm-gamma': { accuracy: 0.93 },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'arms-rollup-2',
      task_title: 'other run',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'arms-rollup-test',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.88, met: false, measurable: true },
      polling_metadata: { result_metrics: { accuracy: 0.88 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job1, job2]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    const rollup = await findByTestId('arms-met-count');
    expect(rollup.textContent).toBe('2/3 arms met');
    // arm-beta failed (0.82 < 0.9), so exactly 2 out of 3 met
    expect(rollup.textContent).not.toContain('3/3');
  });

  test('per-arm pass/fail chip handles != operator correctly', async () => {
    // arm-neq-pass has value 0.5 which != 0.9 (should pass); arm-neq-fail has value 0.9 which == 0.9 (should fail)
    const job = makeJob({
      id: 'neq-job',
      task_title: 'neq run',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'neq-sweep',
      success_criterion: { metric: 'score', operator: '!=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'score',
        operator: '!=',
        threshold: 0.9,
        value: 0.5,
        met: true,
        measurable: true,
        per_arm: { 'arm-neq-pass': 0.5, 'arm-neq-fail': 0.9 },
      },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-neq-pass': { score: 0.5 },
            'arm-neq-fail': { score: 0.9 },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'neq-job2',
      task_title: 'neq run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'neq-sweep',
      success_criterion: { metric: 'score', operator: '!=' as const, threshold: 0.9 },
      criterion_verdict: { metric: 'score', operator: '!=', threshold: 0.9, value: 0.5, met: true, measurable: true },
      polling_metadata: { result_metrics: { score: 0.5 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job, job2]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    await findByTestId('arms-comparison');
    const pfChips = getAllByTestId('arm-passfail-chip');
    const pfTexts = pfChips.map((c) => c.textContent);
    // arm-neq-pass (0.5 != 0.9) → pass; arm-neq-fail (0.9 != 0.9 is false) → fail
    expect(pfTexts).toContain('pass');
    expect(pfTexts).toContain('fail');
    const passChip = pfChips.find((c) => c.textContent === 'pass');
    const failChip = pfChips.find((c) => c.textContent === 'fail');
    expect(passChip?.className).toContain('emerald');
    expect(failChip?.className).toContain('red');
  });

  test('per-arm pass/fail chip renders amber inconclusive when per_arm value is null', async () => {
    const job = makeJob({
      id: 'null-arm-job',
      task_title: 'null arm run',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'null-arm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.9,
        met: true,
        measurable: true,
        per_arm: { 'arm-measured': 0.95, 'arm-unmeasured': null },
      },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-measured': { accuracy: 0.95 },
            'arm-unmeasured': {},
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'null-arm-job2',
      task_title: 'null arm run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'null-arm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.88, met: false, measurable: true },
      polling_metadata: { result_metrics: { accuracy: 0.88 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([job, job2]));
    const { findByTestId, getAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    await findByTestId('arms-comparison');
    const pfChips = getAllByTestId('arm-passfail-chip');
    const pfTexts = pfChips.map((c) => c.textContent);
    expect(pfTexts).toContain('inconclusive');
    expect(pfTexts).toContain('pass');
    const inconclusiveChip = pfChips.find((c) => c.textContent === 'inconclusive');
    expect(inconclusiveChip?.className).toContain('amber');
  });

  test('per-arm pass/fail chip does not render when criterion_verdict has no per_arm but job has arms', async () => {
    const jobWithArmsNoPérArm = makeJob({
      id: 'no-per-arm-job',
      task_title: 'no perarm run',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.5,
      experiment: 'no-perarm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.92,
        met: true,
        measurable: true,
        // no per_arm field
      },
      polling_metadata: {
        result_metrics: {
          arms: {
            'arm-alpha': { accuracy: 0.92, cost_usd: 0.75 },
            'arm-beta': { accuracy: 0.88, cost_usd: 0.75 },
          },
        },
      },
      started_at: null,
      completed_at: null,
    });
    const job2 = makeJob({
      id: 'no-per-arm-job2',
      task_title: 'no perarm run 2',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      experiment: 'no-perarm-sweep',
      success_criterion: { metric: 'accuracy', operator: '>=' as const, threshold: 0.9 },
      criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.88, met: false, measurable: true },
      polling_metadata: { result_metrics: { accuracy: 0.88 } },
      started_at: null,
      completed_at: null,
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([jobWithArmsNoPérArm, job2]));
    const { findByTestId, queryAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggleBtn = await findByTestId('sweep-compare-toggle');
    await act(async () => { toggleBtn.click(); });
    await findByTestId('arms-comparison');
    // pass/fail chips must NOT render when per_arm is absent
    expect(queryAllByTestId('arm-passfail-chip')).toHaveLength(0);
  });

  test('renders grounded answer and citations for Ask about this run', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The experiment achieved 92% accuracy on the validation set.',
      citations: ['gs://artifacts/metrics.json', 'gs://artifacts/eval_log.txt'],
      grounded: true,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Ask input is visible for terminal jobs
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'What accuracy did this run achieve?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    // Grounded answer renders
    const result = await findByTestId('compute-ask-result');
    expect(result.textContent).toContain('92% accuracy');
    // Citations render inline
    const citations = await findByTestId('compute-ask-citations');
    expect(citations.textContent).toContain('gs://artifacts/metrics.json');
    expect(citations.textContent).toContain('gs://artifacts/eval_log.txt');
    // Ungrounded disclaimer is absent for a grounded answer
    expect(result.querySelector('[data-testid="compute-ask-ungrounded"]')).toBeNull();
    // API called with correct args
    expect(askComputeJob).toHaveBeenCalledWith('job-1', 'What accuracy did this run achieve?', undefined);
  });

  test('shows starter questions based on criterion verdict', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.95,
        met: true,
        measurable: true,
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    // Starter question buttons appear
    const starterQuestions = await findAllByTestId('compute-ask-starter');
    expect(starterQuestions.length).toBeGreaterThan(0);
    // Suggestion includes the criterion
    expect(starterQuestions[0].textContent).toContain('accuracy');
  });

  test('clicking starter question populates the input field', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      criterion_verdict: {
        metric: 'accuracy',
        operator: '>=',
        threshold: 0.9,
        value: 0.95,
        met: true,
        measurable: true,
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findAllByTestId, findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    expect(input).toHaveValue('');
    const starterQuestions = await findAllByTestId('compute-ask-starter');
    await act(async () => { starterQuestions[0].click(); });
    // Input now contains the clicked question
    expect(input).toHaveValue(starterQuestions[0].textContent || '');
  });

  test('formats citations with readable labels instead of opaque strings', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The control arm achieved 88% accuracy.',
      citations: ['result_metrics:accuracy', 'arm:control'],
      grounding_miss: false,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'How did control perform?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    const citations = await findByTestId('compute-ask-citations');
    // Citations rendered with formatted labels
    expect(citations.textContent).toContain('result_metrics:');
    expect(citations.textContent).toContain('accuracy');
    expect(citations.textContent).toContain('arm:');
    expect(citations.textContent).toContain('control');
  });

  test('shows "Grounded in run data" only when citations are present', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The accuracy was 92%.',
      citations: ['result_metrics:accuracy'],
      grounding_miss: false,
    });
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'What accuracy?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    const groundedLabel = await findByTestId('compute-ask-grounded');
    expect(groundedLabel.textContent).toContain('Grounded in run data');
    // No ungrounded message when citations present
    expect(queryByTestId('compute-ask-ungrounded')).toBeNull();
  });

  test('shows ungrounded message when no citations and grounding_miss true', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: null,
      citations: [],
      grounding_miss: true,
    });
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'Unknown question' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    const ungroundedMsg = await findByTestId('compute-ask-ungrounded');
    expect(ungroundedMsg.textContent).toContain('No evidence found');
    // Grounded label not shown
    expect(queryByTestId('compute-ask-grounded')).toBeNull();
  });

  test('shows starter questions for multi-arm runs', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      polling_metadata: {
        result_metrics: {
          arms: {
            'control': { accuracy: 0.88 },
            'treatment': { accuracy: 0.92 },
          },
        },
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const starterQuestions = await findAllByTestId('compute-ask-starter');
    // Includes arm-related questions
    const armQuestions = starterQuestions.filter((q) => q.textContent?.toLowerCase().includes('arm'));
    expect(armQuestions.length).toBeGreaterThan(0);
  });

  test('placeholder text suggests first starter question when available', async () => {
    const terminal = makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      criterion_verdict: {
        metric: 'f1_score',
        operator: '>=',
        threshold: 0.85,
        value: 0.88,
        met: true,
        measurable: true,
      },
    });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input') as HTMLInputElement;
    // Placeholder includes first starter question suggestion
    expect(input.placeholder).toContain('f1_score');
  });

  test('shows no-citation-info when answer has no citations and grounding_miss is false', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The run completed successfully.',
      citations: [],
      grounding_miss: false,
    });
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'What happened?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    const noCitationMsg = await findByTestId('compute-ask-no-citation-info');
    expect(noCitationMsg.textContent).toContain('No citations provided');
    // Other messages absent
    expect(queryByTestId('compute-ask-grounded')).toBeNull();
    expect(queryByTestId('compute-ask-ungrounded')).toBeNull();
  });

  test('renders ungrounded compute Q&A answer with an unverified caution treatment', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The model might have been overfitting.',
      citations: [],
      grounding_miss: true,
    });
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'Why did this happen?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    // Result block uses amber warning colors instead of indigo
    const resultBlock = await findByTestId('compute-ask-result');
    expect(resultBlock.className).toContain('bg-amber-500/10');
    expect(resultBlock.className).toContain('border-amber-500/30');
    // Caution label is visible
    const cautionLabel = await findByTestId('compute-ask-unverified-caution');
    expect(cautionLabel.textContent).toContain('Unverified answer');
    expect(cautionLabel.textContent).toContain('no grounding found');
    // Answer text uses amber color
    expect(resultBlock.textContent).toContain('The model might have been overfitting.');
    const answerDiv = resultBlock.querySelector('.text-amber-200');
    expect(answerDiv).toBeTruthy();
  });

  test('multi-turn: both Q&A pairs remain rendered after second question, prior turn sent as history', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.5 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));

    // First answer: grounded
    askComputeJob.mockResolvedValueOnce({
      answer: 'The accuracy was 0.95.',
      citations: ['gs://artifacts/metrics.json#metric:accuracy'],
      grounding_miss: false,
      fallback_reason: null,
    });
    // Second answer: also grounded
    askComputeJob.mockResolvedValueOnce({
      answer: 'The control arm scored 0.85.',
      citations: ['gs://artifacts/metrics.json#arm:control'],
      grounding_miss: false,
      fallback_reason: null,
    });

    const { findByTestId, findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);

    // Ask first question
    const input = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'What was the accuracy?' } });
    });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });

    // First result rendered
    await findByTestId('compute-ask-result');

    // Ask second question (input should be cleared after first answer)
    const input2 = await findByTestId('compute-ask-input');
    await act(async () => {
      fireEvent.change(input2, { target: { value: 'And arm control?' } });
    });
    await act(async () => { submitBtn.click(); });

    // Both Q&A pairs must still be in the DOM
    const results = await findAllByTestId('compute-ask-result');
    expect(results.length).toBe(2);
    expect(results[0].textContent).toContain('0.95');
    expect(results[1].textContent).toContain('0.85');

    // Thread questions rendered
    const questions = await findAllByTestId('compute-ask-thread-question');
    expect(questions.length).toBe(2);

    // Second call must include the prior turn as history
    expect(askComputeJob).toHaveBeenNthCalledWith(
      2,
      'job-1',
      'And arm control?',
      [{ question: 'What was the accuracy?', answer: 'The accuracy was 0.95.' }],
    );
  });

  test('multi-turn: per-answer grounding treatment preserved (first grounded, second unverified)', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.5 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));

    askComputeJob.mockResolvedValueOnce({
      answer: 'Accuracy was 0.91.',
      citations: ['gs://artifacts/metrics.json#metric:accuracy'],
      grounding_miss: false,
      fallback_reason: null,
    });
    askComputeJob.mockResolvedValueOnce({
      answer: 'Cannot find evidence for that.',
      citations: [],
      grounding_miss: true,
      fallback_reason: null,
    });

    const { findByTestId, findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);

    const input = await findByTestId('compute-ask-input');
    await act(async () => { fireEvent.change(input, { target: { value: 'First question' } }); });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });

    await findByTestId('compute-ask-grounded');

    const input2 = await findByTestId('compute-ask-input');
    await act(async () => { fireEvent.change(input2, { target: { value: 'Second question' } }); });
    await act(async () => { submitBtn.click(); });

    const results = await findAllByTestId('compute-ask-result');
    expect(results.length).toBe(2);
    // First is grounded (indigo style)
    expect(results[0].className).toContain('indigo');
    // Second is unverified (amber style)
    expect(results[1].className).toContain('amber');

    // Ungrounded notice on second turn
    const ungrounded = results[1].querySelector('[data-testid="compute-ask-unverified-caution"]');
    expect(ungrounded).toBeTruthy();
  });

  test('multi-turn: clear conversation button resets thread', async () => {
    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.5 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'Some answer.',
      citations: ['gs://artifacts/metrics.json#metric:x'],
      grounding_miss: false,
      fallback_reason: null,
    });

    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const input = await findByTestId('compute-ask-input');
    await act(async () => { fireEvent.change(input, { target: { value: 'A question' } }); });
    const submitBtn = await findByTestId('compute-ask-submit');
    await act(async () => { submitBtn.click(); });
    await findByTestId('compute-ask-result');

    // Clear button appears
    const clearBtn = await findByTestId('compute-ask-clear');
    await act(async () => { clearBtn.click(); });

    // Thread must be gone after clearing
    expect(queryByTestId('compute-ask-result')).toBeNull();
    expect(queryByTestId('compute-ask-thread')).toBeNull();
  });

  test('shows experiment identity section for active jobs with experiment, hypothesis, and criterion', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        experiment: 'model-accuracy-sweep',
        hypothesis: 'Increasing batch size improves convergence speed without harming final accuracy',
        success_criterion: {
          metric: 'accuracy',
          operator: '>=',
          threshold: 0.92,
        },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);

    // Verify experiment identity section renders for active job
    const identitySection = await findByTestId('compute-experiment-identity');
    expect(identitySection.textContent).toContain('model-accuracy-sweep');
    expect(identitySection.textContent).toContain('Increasing batch size improves convergence speed');
    expect(identitySection.textContent).toContain('accuracy >= 0.92');
  });

  test('ask-this-run threads persist across panel visits via localStorage', async () => {
    // Clear only the key we'll use for this test
    const testKey = 'blueprint-admin:compute-ask-state:test-proj-persist';
    localStorage.removeItem(testKey);

    const terminal = makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 });
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));
    askComputeJob.mockResolvedValue({
      answer: 'The run succeeded because all metrics met the criterion.',
      citations: ['criterion_verdict:accuracy=0.95'],
      grounding_miss: false,
      fallback_reason: null,
    });

    // Render 1: ask a question and get a response using a unique project ID
    const { findByTestId: findByTestId1, unmount: unmount1 } = render(
      <ComputeJobsPanel projectId="test-proj-persist" />
    );
    const input1 = await findByTestId1('compute-ask-input');
    await act(async () => { fireEvent.change(input1, { target: { value: 'Why did this succeed?' } }); });
    const submitBtn1 = await findByTestId1('compute-ask-submit');
    await act(async () => { submitBtn1.click(); });

    // Wait for the response to appear in the thread
    await findByTestId1('compute-ask-thread');

    // Verify localStorage has the question + response saved
    const storedState1 = localStorage.getItem(testKey);
    expect(storedState1).toBeTruthy();
    const parsed1 = JSON.parse(storedState1!);
    expect(parsed1['job-1']).toBeTruthy();
    expect(parsed1['job-1'].thread).toHaveLength(1);
    expect(parsed1['job-1'].thread[0].question).toBe('Why did this succeed?');
    expect(parsed1['job-1'].thread[0].result.answer).toContain('metrics met');

    // Unmount the component
    unmount1();

    // Reset the fetch mock for the second render
    fetchComputeJobs.mockClear();
    fetchComputeJobs.mockResolvedValue(wrapJobs([terminal]));

    // Render 2: component should restore the thread from localStorage
    const { findByTestId: findByTestId2 } = render(
      <ComputeJobsPanel projectId="test-proj-persist" />
    );

    // Thread should be visible immediately without asking again
    const thread = await findByTestId2('compute-ask-thread');
    expect(thread.textContent).toContain('Why did this succeed?');
    expect(thread.textContent).toContain('The run succeeded because all metrics met the criterion.');

    // Verify localStorage still has the same state
    const storedState2 = localStorage.getItem(testKey);
    expect(storedState2).toBe(storedState1);

    // Cleanup
    localStorage.removeItem(testKey);
  });
});

describe('ComputeJobsPanel — draft provenance', () => {
  beforeEach(() => {
    fetchComputeJobs.mockReset();
    killComputeJob.mockReset();
    analyzeComputeJob.mockReset();
    rerunComputeExperiment.mockReset();
    askComputeJob.mockReset();
  });

  test('renders provenance block when operator_idea_text is present', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        launch_params: {
          provenance: {
            idea: 'Fine-tune LR on ImageNet',
            draft_notes: 'Suggested 0.01 as the optimal learning rate based on literature.',
            drafted_from_refinement: true,
          },
        },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-draft-provenance');
    expect(block.textContent).toContain('Fine-tune LR on ImageNet');
    expect(block.textContent).toContain('Suggested 0.01 as the optimal learning rate');
    expect(block.textContent).toContain('From AI draft');
  });

  test('renders provenance block with only idea text when llm_draft_notes is absent', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        launch_params: {
          provenance: {
            idea: 'Test batch size scaling',
            drafted_from_refinement: false,
          },
        },
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const block = await findByTestId('compute-draft-provenance');
    expect(block.textContent).toContain('Test batch size scaling');
    expect(block.textContent).not.toContain('From AI draft');
  });

  test('does not render provenance block when neither idea nor notes are present', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({ status: 'SUCCEEDED', outcome: 'SUCCESS', final_cost_usd: 1.0 }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-draft-provenance')).toBeNull();
  });
});
describe('ComputeJobsPanel — viewable artifact gallery', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    resolveArtifactDownloadUrl.mockReset();
  });

  const VIEWABLE = [
    { filename: 'loss_curve.png', storage_uri: 'gs://bucket/compute-jobs/job-1/artifacts/viewable/0-loss_curve.png', content_type: 'image/png', kind: 'image' as const, size_bytes: 41233 },
    { filename: 'rollout.mp4', storage_uri: 'gs://bucket/compute-jobs/job-1/artifacts/viewable/1-rollout.mp4', content_type: 'video/mp4', kind: 'video' as const, size_bytes: 2_400_000 },
  ];

  function makeGalleryJob(): ComputeJob {
    return makeJob({
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 1.0,
      viewable_artifacts: VIEWABLE,
    });
  }

  test('renders collapsed gallery from viewable_artifacts without resolving URLs', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeGalleryJob()]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggle = await findByTestId('compute-artifact-gallery-toggle');
    expect(toggle.textContent).toContain('Show previews (2)');
    // Lazy: nothing resolved and no thumbnails until the gallery is expanded.
    expect(resolveArtifactDownloadUrl).not.toHaveBeenCalled();
    expect(queryByTestId('compute-viewable-artifact')).toBeNull();
  });

  test('expanding the gallery resolves each storage_uri and renders image + video previews', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeGalleryJob()]));
    resolveArtifactDownloadUrl.mockImplementation(async (_projectId: string, uri: string) =>
      `https://signed.example/${uri.split('/').pop()}`);
    const { findByTestId, findAllByTestId, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggle = await findByTestId('compute-artifact-gallery-toggle');
    await act(async () => { toggle.click(); });

    const items = await findAllByTestId('compute-viewable-artifact');
    expect(items).toHaveLength(2);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(2);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', VIEWABLE[0].storage_uri);
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', VIEWABLE[1].storage_uri);

    await waitFor(() => {
      const img = container.querySelector('img[alt="loss_curve.png"]');
      expect(img?.getAttribute('src')).toBe('https://signed.example/0-loss_curve.png');
    });
    const video = await findByTestId('compute-artifact-video');
    expect(video.tagName).toBe('VIDEO');
    expect(video.hasAttribute('controls')).toBe(true);
    expect(video.getAttribute('src')).toBe('https://signed.example/1-rollout.mp4');
    // Captions carry name + size.
    const gallery = await findByTestId('compute-artifact-gallery');
    expect(gallery.textContent).toContain('loss_curve.png · 40 KB');
    expect(gallery.textContent).toContain('rollout.mp4 · 2.3 MB');
  });

  test('resolution failure shows a quiet placeholder that can retry', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([makeGalleryJob()]));
    resolveArtifactDownloadUrl.mockRejectedValue(new Error('403'));
    const { findByTestId, findAllByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggle = await findByTestId('compute-artifact-gallery-toggle');
    await act(async () => { toggle.click(); });

    const placeholders = await findAllByTestId('compute-artifact-placeholder');
    expect(placeholders).toHaveLength(2);
    expect(placeholders[0].textContent).toContain('preview unavailable');

    // Retry re-resolves just the clicked artifact and swaps in the thumbnail.
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed.example/retried.png');
    await act(async () => { placeholders[0].click(); });
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledTimes(3);
    await waitFor(async () => {
      const thumb = await findByTestId('compute-artifact-thumbnail');
      expect(thumb.querySelector('img')?.getAttribute('src')).toBe('https://signed.example/retried.png');
    });
  });

  test('renders a pdf artifact as a click-to-open tile (no inline img/video) that opens a new tab', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(
      { opener: null, location: { replace: vi.fn() }, close: vi.fn() } as never,
    );
    const pdfUri = 'gs://bucket/compute-jobs/job-1/artifacts/viewable/2-report.pdf';
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        viewable_artifacts: [
          { filename: 'report.pdf', storage_uri: pdfUri, content_type: 'application/pdf', kind: 'pdf' as const, size_bytes: 88_000 },
        ],
      }),
    ]));
    resolveArtifactDownloadUrl.mockResolvedValue('https://signed.example/2-report.pdf');
    const { findByTestId, queryByTestId, container } = render(<ComputeJobsPanel projectId="proj-1" />);
    const toggle = await findByTestId('compute-artifact-gallery-toggle');
    expect(toggle.textContent).toContain('Show previews (1)');
    await act(async () => { toggle.click(); });

    // A pdf tile renders — never an inline <img>/<video>, and not resolved eagerly.
    const tile = await findByTestId('compute-artifact-pdf');
    expect(tile.tagName).toBe('BUTTON');
    expect(tile.textContent).toContain('Open PDF');
    expect(container.querySelector('img')).toBeNull();
    expect(queryByTestId('compute-artifact-video')).toBeNull();
    expect(resolveArtifactDownloadUrl).not.toHaveBeenCalled();
    // Caption carries the filename + size.
    const gallery = await findByTestId('compute-artifact-gallery');
    expect(gallery.textContent).toContain('report.pdf · 86 KB');

    // Clicking resolves a fresh signed URL and opens it in a new tab.
    await act(async () => { tile.click(); });
    await waitFor(() => expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith('proj-1', pdfUri));
    expect(openSpy).toHaveBeenCalled();
    openSpy.mockRestore();
  });

  test('no gallery section when viewable_artifacts is absent or malformed', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        viewable_artifacts: [{ kind: 'image' }, 'nonsense', null] as unknown as ComputeJob['viewable_artifacts'],
      }),
    ]));
    const { findByText, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByText('compute_experiment: sweep');
    expect(queryByTestId('compute-artifact-gallery')).toBeNull();
  });
});

describe('ComputeJobsPanel — live output expander', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
  });

  test('clips active live output to 30 lines with a Show-all expander', async () => {
    const lines = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n');
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        polling_metadata: { live_output: { text: lines, truncated: false } },
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const showAllBtn = await findByTestId('compute-live-output-show-all');
    expect(showAllBtn.textContent).toContain('50 lines');
    // Only the last 30 lines visible before expanding.
    const block = await findByTestId('compute-live-output');
    expect(block.textContent).not.toContain('line 0');
    expect(block.textContent).toContain('line 49');

    await act(async () => { showAllBtn.click(); });
    const expanded = await findByTestId('compute-live-output');
    expect(expanded.textContent).toContain('line 0');
    expect(queryByTestId('compute-live-output-show-all')).toBeNull();
  });

  test('no live-output expander when output fits within 30 lines', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        polling_metadata: { live_output: { text: 'line1\nline2\n', truncated: false } },
      }),
    ]));
    const { findByTestId, queryByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    await findByTestId('compute-live-output');
    expect(queryByTestId('compute-live-output-show-all')).toBeNull();
  });
});

describe('ComputeJobsPanel — runner identity on finished jobs', () => {
  beforeEach(() => {
    fetchComputeJobs.mockReset();
    killComputeJob.mockReset();
    resolveArtifactDownloadUrl.mockReset();
  });

  test('renders runner identity on finished job without claim timestamp', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        runner_target_id: 'runner-abc12345-dead-beef-0000-111122223333',
        runner: { hostname: 'my-gpu-host', id: 'runner-abc12345-dead-beef-0000-111122223333', status: null },
        runner_claimed_at: null,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const runnerEl = await findByTestId('compute-runner-claimed');
    expect(runnerEl.textContent).toContain('my-gpu-host');
    expect(runnerEl.textContent).not.toContain('claimed');
  });

  test('renders runner identity using truncated target id when hostname absent', async () => {
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'SUCCEEDED',
        outcome: 'SUCCESS',
        final_cost_usd: 1.0,
        runner_target_id: 'runner-abc12345-dead-beef-0000-111122223333',
        runner: null,
        runner_claimed_at: null,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const runnerEl = await findByTestId('compute-runner-claimed');
    // The component truncates the target id to 8 chars (`slice(0, 8)`) — the
    // long-standing behavior on both branches. Assert the truncated form is
    // rendered AND that the full id is not, so this genuinely covers truncation.
    expect(runnerEl.textContent).toContain('runner-a');
    expect(runnerEl.textContent).not.toContain('runner-abc12345-dead-beef-0000-111122223333');
    expect(runnerEl.textContent).not.toContain('claimed');
  });

  test('renders claimed suffix when runner_claimed_at is present', async () => {
    const claimedAt = new Date(Date.now() - 60_000).toISOString();
    fetchComputeJobs.mockResolvedValue(wrapJobs([
      makeJob({
        status: 'RUNNING',
        runner_target_id: 'runner-abc12345-dead-beef-0000-111122223333',
        runner: { hostname: 'my-gpu-host', id: 'runner-abc12345-dead-beef-0000-111122223333', status: null },
        runner_claimed_at: claimedAt,
      }),
    ]));
    const { findByTestId } = render(<ComputeJobsPanel projectId="proj-1" />);
    const runnerEl = await findByTestId('compute-runner-claimed');
    expect(runnerEl.textContent).toContain('my-gpu-host');
    expect(runnerEl.textContent).toContain('claimed');
    expect(runnerEl.textContent).toContain('ago');
  });
});

describe('CompactJobsTable — evidence tier + runner identity', () => {
  test('terminal job renders the server-provided tier badge and runner hostname', () => {
    const job = makeJob({
      id: 'job-local',
      backend: 'runner-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 0,
      evidence_tier: 'covered',
      runner_target_id: 'dd59526e-0001-0002-0003-000000000004',
      runner: { id: 'dd59526e-0001-0002-0003-000000000004', hostname: 'iconlab-gpu-01', status: 'active' },
    });
    const { getByTestId, container } = render(
      <CompactJobsTable jobs={[job]} sortConfig={null} onSort={() => {}} />,
    );
    // Runner identity surfaces via the dedicated chip.
    expect(getByTestId('compact-table-runner').textContent).toContain('iconlab-gpu-01');
    // The badge reflects the canonical server tier ('covered' → "Machine verified"),
    // not a client re-derivation.
    expect(container.textContent).toContain('Machine verified');
  });

  test('runner chip falls back to a truncated target id when hostname is absent', () => {
    const job = makeJob({
      id: 'job-local-2',
      backend: 'runner-subprocess',
      status: 'SUCCEEDED',
      outcome: 'SUCCESS',
      final_cost_usd: 0,
      evidence_tier: 'partial',
      runner_target_id: 'abcdef12-0000-0000-0000-000000000000',
      runner: null,
    });
    const { getByTestId } = render(
      <CompactJobsTable jobs={[job]} sortConfig={null} onSort={() => {}} />,
    );
    const chip = getByTestId('compact-table-runner');
    expect(chip.textContent).toContain('abcdef12');
    expect(chip.textContent).not.toContain('abcdef12-0000-0000-0000-000000000000');
  });

  test('active job suppresses the tier badge but still shows its runner', () => {
    const job = makeJob({
      id: 'job-active',
      backend: 'runner-subprocess',
      status: 'RUNNING',
      outcome: null,
      completed_at: null,
      evidence_tier: 'covered',
      runner_target_id: 'aa000000-0000-0000-0000-000000000001',
      runner: { id: 'aa000000-0000-0000-0000-000000000001', hostname: 'iconlab-gpu-02', status: 'active' },
    });
    const { getByTestId, container } = render(
      <CompactJobsTable jobs={[job]} sortConfig={null} onSort={() => {}} />,
    );
    expect(getByTestId('compact-table-runner').textContent).toContain('iconlab-gpu-02');
    // Badge is terminal-only, consistent with the full job card.
    expect(container.textContent).not.toContain('Machine verified');
  });
});

describe('ComputeJobsPanel evidence-tier filter + sort', () => {
  afterEach(() => {
    fetchComputeJobs.mockReset();
    fetchComputeJobsAggregateSummary.mockReset();
    fetchHeldComputeExperiments.mockReset();
    fetchAwaitingLocalRunnerHolds.mockReset();
    fetchComputeSweepGroups.mockReset();
  });

  // A mix spanning the two coarse trust groups plus an active job (no tier).
  //  - tierjob-alpha:   operator hardware + a result → 'covered' → machine-verified
  //  - tierjob-bravo:  cloud + a result             → 'attested' → low-trust
  //  - tierjob-charlie: no backend, no result        → 'uncovered' → low-trust
  //  - tierjob-delta:    still running                 → no tier (excluded by any filter)
  const coveredJob = makeJob({
    id: 'tierjob-alpha',
    task_id: 'task-covered',
    task_title: 'tierjob-alpha',
    backend: 'local-subprocess',
    status: 'SUCCEEDED',
    outcome: 'SUCCESS',
    per_arm_verdicts: { armA: { value: 0.91, met: true, cost_usd: null } },
    accrued_cost_usd: 0,
    final_cost_usd: 0,
    completed_at: new Date().toISOString(),
  });
  const attestedJob = makeJob({
    id: 'tierjob-bravo',
    task_id: 'task-attested',
    task_title: 'tierjob-bravo',
    backend: 'gcp',
    status: 'SUCCEEDED',
    outcome: 'SUCCESS',
    criterion_verdict: { metric: 'acc', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
    accrued_cost_usd: 1,
    final_cost_usd: 1,
    completed_at: new Date().toISOString(),
  });
  const uncoveredJob = makeJob({
    id: 'tierjob-charlie',
    task_id: 'task-uncovered',
    task_title: 'tierjob-charlie',
    backend: '',
    status: 'SUCCEEDED',
    outcome: 'SUCCESS',
    criterion_verdict: null,
    per_arm_verdicts: null,
    accrued_cost_usd: 0,
    final_cost_usd: 0,
    completed_at: new Date().toISOString(),
  });
  const activeJob = makeJob({
    id: 'tierjob-delta',
    task_id: 'task-active',
    task_title: 'tierjob-delta',
    backend: 'local-subprocess',
    status: 'RUNNING',
    outcome: null,
    completed_at: null,
  });

  function mountWith(jobs: ComputeJob[]) {
    fetchComputeJobs.mockResolvedValue(wrapJobs(jobs));
    fetchComputeJobsAggregateSummary.mockResolvedValue(null);
    fetchHeldComputeExperiments.mockResolvedValue([]);
    fetchAwaitingLocalRunnerHolds.mockResolvedValue([]);
    fetchComputeSweepGroups.mockResolvedValue({ groups: [], has_more: false });
    return render(<ComputeJobsPanel projectId="proj-1" />);
  }

  test('renders the tier filter control in the filter bar', async () => {
    const { findByTestId } = mountWith([coveredJob]);
    const control = await findByTestId('compute-tier-filter');
    expect(control).toBeTruthy();
    // Both coarse groupings the operator asked for are present, plus the default.
    expect(control.querySelector('[data-testid="compute-tier-all"]')).toBeTruthy();
    expect(control.querySelector('[data-testid="compute-tier-machine-verified"]')).toBeTruthy();
    expect(control.querySelector('[data-testid="compute-tier-low-trust"]')).toBeTruthy();
  });

  test('low-trust filter hides machine-verified jobs and keeps attested/self-reported ones', async () => {
    const { findByTestId, getByTestId } = mountWith([coveredJob, attestedJob, uncoveredJob, activeJob]);
    const jobsList = await findByTestId('compute-jobs-list');
    // Default: everything visible.
    await waitFor(() => expect(jobsList.textContent).toContain('tierjob-alpha'));
    expect(jobsList.textContent).toContain('tierjob-bravo');

    act(() => { fireEvent.click(getByTestId('compute-tier-low-trust')); });

    await waitFor(() => expect(jobsList.textContent).not.toContain('tierjob-alpha'));
    // low-trust keeps the non-machine-verified terminal jobs...
    expect(jobsList.textContent).toContain('tierjob-bravo');
    expect(jobsList.textContent).toContain('tierjob-charlie');
    // ...and excludes active jobs (they carry no tier).
    expect(jobsList.textContent).not.toContain('tierjob-delta');
  });

  test('machine-verified filter shows only covered jobs', async () => {
    const { findByTestId, getByTestId } = mountWith([coveredJob, attestedJob, uncoveredJob, activeJob]);
    const jobsList = await findByTestId('compute-jobs-list');
    await waitFor(() => expect(jobsList.textContent).toContain('tierjob-bravo'));

    act(() => { fireEvent.click(getByTestId('compute-tier-machine-verified')); });

    await waitFor(() => expect(jobsList.textContent).toContain('tierjob-alpha'));
    expect(jobsList.textContent).not.toContain('tierjob-bravo');
    expect(jobsList.textContent).not.toContain('tierjob-charlie');
    expect(jobsList.textContent).not.toContain('tierjob-delta');
  });

  test('tier sort orders the compact table from highest to lowest trust', async () => {
    const { findByTestId, findByText, getAllByTestId } = mountWith([uncoveredJob, coveredJob, attestedJob]);
    // Switch to the compact table view.
    await findByText('tierjob-alpha');
    const toggleBtn = await findByTestId('compute-view-mode-toggle');
    await act(async () => { toggleBtn.click(); });
    await findByTestId('compact-jobs-table');

    // Click the new Tier sort header (first click → ascending = highest trust first).
    const [tierHeader] = getAllByTestId('jobs-table-sort-tier');
    act(() => { fireEvent.click(tierHeader); });

    await waitFor(() => {
      const order = getAllByTestId('compact-table-job-name').map((n) => n.textContent);
      expect(order).toEqual(['tierjob-alpha', 'tierjob-bravo', 'tierjob-charlie']);
    });
  });
});
