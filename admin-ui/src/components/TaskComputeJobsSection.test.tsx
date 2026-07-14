import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import { TaskComputeJobsSection } from './TaskDetailDrawerSections';
import type { Task } from '../types/task';
import type { ComputeJob } from '../api';
import { fetchComputeJobs } from '../api';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    API_BASE_URL: 'http://localhost:8000',
    fetchComputeJobs: vi.fn(async () => ({
      jobs: [],
      hasMore: false,
      summary: { total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50,
      offset: 0,
    })),
    fetchKnowledgeDocument: vi.fn(async () => ({ id: 'doc-1', body: '' })),
    resolveArtifactDownloadUrl: vi.fn(async () => ''),
    resolveViewportFailureDownloadUrl: vi.fn(async () => ''),
    resolveViewportShotDownloadUrl: vi.fn(async () => ''),
  });
});

const baseJob: ComputeJob = {
  id: 'job-abc123',
  project_id: 'proj-1',
  task_id: 'task-compute-1',
  backend: 'local-subprocess',
  external_compute_id: null,
  machine_type: null,
  status: 'SUCCEEDED',
  terminal_reason: null,
  outcome: 'SUCCESS',
  spend_cap_usd: 5.0,
  wall_clock_deadline: null,
  created_at: '2026-07-11T10:00:00Z',
  updated_at: '2026-07-11T10:30:00Z',
  started_at: '2026-07-11T10:00:10Z',
  completed_at: '2026-07-11T10:29:50Z',
  last_polled_at: '2026-07-11T10:29:50Z',
  accrued_cost_usd: 0,
  final_cost_usd: 0,
  result_reference: null,
  accelerator_type: null,
  accelerator_count: null,
  task_title: 'GPU training run',
  task_type: 'compute_experiment',
  runner_target_id: null,
  runner_claimed_at: null,
};

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-compute-1',
    title: 'GPU training run',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'compute_experiment' as Task['task_type'],
    created_at: '2026-07-11T10:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    project_id: 'proj-1',
    payloads: [{ type: 'compute_experiment', command: 'python train.py' }],
    plan_output: {
      knowledge_document: {
        id: 'doc-1',
        title: 'CUDA benchmark result',
        source_kind: 'compute_experiment',
        compute_acceptance_grade: 'acceptance',
        compute_acceptance_reason: 'llm_artifact_cited',
        analysis_mode: 'llm',
        unavailable_cause: null,
        analysis_fallback_reason: null,
      },
    },
    ...overrides,
  } as Task;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('TaskComputeJobsSection — evidence tier', () => {
  it('renders the per-job EvidenceTierBadge reflecting compute provenance', async () => {
    // Operator hardware + a measured criterion verdict => "covered" => "Machine verified".
    const job: ComputeJob = {
      ...baseJob,
      criterion_verdict: {
        metric: 'accuracy', operator: '>=', threshold: 0.9,
        value: 0.95, met: true, measurable: true,
      },
    };
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce({
      jobs: [job], hasMore: false,
      summary: { total_jobs: 1, active_jobs: 0, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 1, criterion_met: 1 },
      limit: 50, offset: 0,
    });

    render(<TaskComputeJobsSection task={makeTask()} />);
    await waitFor(() => expect(screen.getByText('Machine verified')).toBeInTheDocument());
    // The acceptance-grade row is now labelled accurately, NOT "Evidence tier".
    expect(screen.queryByText('Evidence tier')).not.toBeInTheDocument();
    expect(screen.getByText('Acceptance grade')).toBeInTheDocument();
  });

  it('demotes a local run with no measured result to "Partial", not "Machine verified"', async () => {
    // Operator hardware but no criterion verdict => "partial".
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce({
      jobs: [baseJob], hasMore: false,
      summary: { total_jobs: 1, active_jobs: 0, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50, offset: 0,
    });

    render(<TaskComputeJobsSection task={makeTask()} />);
    await waitFor(() => expect(screen.getByText('Partial')).toBeInTheDocument());
    expect(screen.queryByText('Machine verified')).not.toBeInTheDocument();
  });

  it('shows "Attested" evidence tier for a cloud-backend job', async () => {
    const cloudJob: ComputeJob = { ...baseJob, backend: 'gcp' };
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce({
      jobs: [cloudJob], hasMore: false,
      summary: { total_jobs: 1, active_jobs: 0, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50, offset: 0,
    });

    render(<TaskComputeJobsSection task={makeTask()} />);
    await waitFor(() => expect(screen.getByText('Attested')).toBeInTheDocument());
  });
});
