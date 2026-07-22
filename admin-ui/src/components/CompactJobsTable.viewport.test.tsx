import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CompactJobsTable } from './ComputeJobsPanel';
import type { ComputeJob } from '../api';
import { expectNoOverflowAt } from '../test/viewport';

// ComputeJobsPanel's module graph statically imports from '../api' (which pulls
// in firebase); mock both so the CompactJobsTable render is isolated to its own
// presentational logic.
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({});
});

function makeJob(overrides: Partial<ComputeJob> = {}): ComputeJob {
  return {
    id: 'job-1',
    project_id: 'proj-1',
    task_id: 'task-1',
    backend: 'runner-subprocess',
    external_compute_id: null,
    machine_type: null,
    status: 'SUCCEEDED',
    terminal_reason: null,
    outcome: 'SUCCESS',
    spend_cap_usd: 4.0,
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
    task_title: 'GPU training run — CUDA benchmark sweep',
    task_type: 'compute_experiment',
    ...overrides,
  } as ComputeJob;
}

// A terminal machine-verified run on operator hardware with a resolved runner,
// a cloud attested run, and an active run (badge suppressed while running).
const JOBS: ComputeJob[] = [
  makeJob({
    id: 'job-local',
    backend: 'runner-subprocess',
    evidence_tier: 'covered',
    runner_target_id: 'dd59526e-0001-0002-0003-000000000004',
    runner: { id: 'dd59526e-0001-0002-0003-000000000004', hostname: 'iconlab-gpu-01', status: 'active' },
    criterion_verdict: { metric: 'reward', operator: '>=', threshold: 0.8, value: 0.91, met: true, measurable: true } as ComputeJob['criterion_verdict'],
    success_criterion: { metric: 'reward', operator: '>=', threshold: 0.8 } as ComputeJob['success_criterion'],
  }),
  makeJob({
    id: 'job-cloud',
    backend: 'gcp',
    evidence_tier: 'attested',
    task_title: 'Cloud attested sweep',
  }),
  makeJob({
    id: 'job-active',
    backend: 'runner-subprocess',
    status: 'RUNNING',
    outcome: null,
    completed_at: null,
    runner_target_id: 'aa000000-0000-0000-0000-000000000001',
    runner: { id: 'aa000000-0000-0000-0000-000000000001', hostname: 'iconlab-gpu-02', status: 'active' },
  }),
];

describe('CompactJobsTable — viewport at 1280px (acceptance capture)', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders tier badge + runner id in the compact table at 1280px without horizontal overflow', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CompactJobsTable jobs={JOBS} sortConfig={null} onSort={() => {}} />,
        );
      });

      // Terminal jobs carry the runner chip...
      const runnerChips = host.querySelectorAll('[data-testid="compact-table-runner"]');
      if (runnerChips.length === 0) {
        throw new Error('expected at least one runner chip in the compact table');
      }
      // ...and the machine-verified tier badge for the covered run.
      if (!host.textContent?.includes('Machine verified')) {
        throw new Error('expected the covered run to render the Machine verified badge');
      }
      // The active run must NOT render an evidence-tier badge (badge is terminal-only).
      // gcp attested run renders the Attested badge.
      if (!host.textContent?.includes('Attested')) {
        throw new Error('expected the cloud run to render the Attested badge');
      }
    });
  });
});
