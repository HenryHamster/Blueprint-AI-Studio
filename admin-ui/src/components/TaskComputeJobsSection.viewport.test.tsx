import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskComputeJobsSection } from './TaskDetailDrawerSections';
import type { Task } from '../types/task';
import type { ComputeJob } from '../api';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

const mockJob: ComputeJob = {
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
  runner_target_id: 'dd59526e-0001-0002-0003-000000000004',
  runner_claimed_at: '2026-07-11T10:00:05Z',
  runner: {
    id: 'dd59526e-0001-0002-0003-000000000004',
    hostname: 'iconlab-gpu-01',
    status: 'active',
  },
  viewable_artifacts: [
    { storage_uri: 'gs://bucket/loss.png', filename: 'loss.png', content_type: 'image/png', size_bytes: 4096, kind: 'image' },
    { storage_uri: 'gs://bucket/rollout.mp4', filename: 'rollout.mp4', content_type: 'video/mp4', size_bytes: 7_000_000, kind: 'video' },
  ],
};

// TaskDetailDrawerSections' module graph statically imports many named exports
// from '../api' (e.g. fetchTaskSerialConsole via TaskDetailDrawerDiagnostics);
// browser-mode ESM requires the mock factory to expose EVERY export the graph
// imports or the linker fails at import time. createApiMock provides the
// complete, typecheck-enforced export surface so new api.ts exports can't
// silently break this factory again.
vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    API_BASE_URL: 'http://localhost:8000',
    fetchComputeJobs: vi.fn(async () => ({
      jobs: [mockJob],
      hasMore: false,
      summary: { total_jobs: 1, active_jobs: 0, terminal_jobs: 1, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 },
      limit: 50,
      offset: 0,
    })),
    fetchKnowledgeDocument: vi.fn(async () => ({ id: 'doc-1', body: '' })),
    resolveArtifactDownloadUrl: vi.fn(async () => ''),
    resolveViewportFailureDownloadUrl: vi.fn(async () => ''),
    resolveViewportShotDownloadUrl: vi.fn(async () => ''),
  });
});

const computeTask: Task = {
  id: 'task-compute-1',
  title: 'GPU training run — CUDA benchmark',
  description: 'Run the benchmark on operator GPU hardware',
  status: 'COMPLETED',
  task_type: 'compute_experiment' as Task['task_type'],
  created_at: '2026-07-11T10:00:00Z',
  created_by: 'USER',
  requires_approval: false,
  project_id: 'proj-1',
  payloads: [
    {
      type: 'compute_experiment',
      command: 'python train.py --epochs 10',
      spend_cap_usd: 5.0,
      deadline_seconds: 3600,
    },
  ],
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
};

describe('TaskComputeJobsSection — viewport regression at 640px', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders provenance + evidence tier at 640px without horizontal overflow', async () => {
    await expectNoOverflowAt(640, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<TaskComputeJobsSection task={computeTask} />); });
      // Wait for fetchComputeJobs effect to resolve
      await act(async () => { await Promise.resolve(); });
      // Must render the evidence-tier badge (local-subprocess + no result → 'partial' → 'Partial')
      if (!(host.textContent ?? '').includes('Partial')) {
        throw new Error('Evidence-tier badge not rendered in TaskComputeJobsSection');
      }
    });
  });
});

describe('TaskComputeJobsSection — viewport at 1280px (acceptance capture)', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders the compute-artifact gallery toggle at 1280px without horizontal overflow', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<TaskComputeJobsSection task={computeTask} />); });
      // Wait for fetchComputeJobs effect to resolve
      await act(async () => { await Promise.resolve(); });
      // The job carries viewable_artifacts, so the drawer gallery toggle mounts.
      if (!host.querySelector('[data-testid="drawer-artifact-gallery-toggle"]')) {
        throw new Error('expected compute-artifact gallery toggle to render');
      }
    });
  });
});
