import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';
import type { ComputeJob } from '../api';

const stressJobs: ComputeJob[] = [
  {
    id: 'job-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    project_id: 'proj-1',
    task_id: 'task-running-1',
    backend: 'gcp_compute_engine',
    external_compute_id: 'compute-job-vm-aaaaaaaa',
    machine_type: 'e2-standard-2',
    status: 'RUNNING',
    terminal_reason: null,
    outcome: null,
    spend_cap_usd: 5.0,
    wall_clock_deadline: new Date(Date.now() + 3 * 3600 * 1000 + 12 * 60 * 1000).toISOString(),
    created_at: new Date(Date.now() - 120_000).toISOString(),
    updated_at: new Date(Date.now() - 15_000).toISOString(),
    started_at: new Date(Date.now() - 110_000).toISOString(),
    completed_at: null,
    last_polled_at: new Date(Date.now() - 15_000).toISOString(),
    accrued_cost_usd: 4.62,
    final_cost_usd: null,
    result_reference: null,
    task_title:
      'compute_experiment: long-running hyperparameter sweep across the entire candidate space for the new ranking model',
    task_type: 'compute_experiment',
  },
  {
    id: 'job-11111111-2222-3333-4444-555555555555',
    project_id: 'proj-1',
    task_id: 'task-killed-1',
    backend: 'gcp_compute_engine',
    external_compute_id: 'compute-job-vm-11111111',
    machine_type: 'e2-standard-2',
    status: 'CANCELLED',
    terminal_reason: 'killed by operator (operator@example.com)',
    outcome: 'CANCELLED',
    spend_cap_usd: 2.0,
    wall_clock_deadline: new Date(Date.now() - 60_000).toISOString(),
    created_at: new Date(Date.now() - 600_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 590_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 0.87,
    final_cost_usd: 0.87,
    result_reference: 'https://storage.example.com/jobs/job-2/result.json',
    task_title: 'compute_experiment: aborted batch eval run',
    task_type: 'compute_experiment',
  },
];

const criterionJobs: ComputeJob[] = [
  {
    id: 'job-crit-met',
    project_id: 'proj-1',
    task_id: 'task-crit-met',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-2',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 600_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 590_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 1.5,
    final_cost_usd: 1.5,
    result_reference: null,
    task_title: 'Benchmark: accuracy >= 0.9',
    task_type: 'compute_experiment',
    success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.95, met: true, measurable: true },
  },
  {
    id: 'job-crit-unmet',
    project_id: 'proj-1',
    task_id: 'task-crit-unmet',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-2',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 700_000).toISOString(),
    updated_at: new Date(Date.now() - 400_000).toISOString(),
    started_at: new Date(Date.now() - 690_000).toISOString(),
    completed_at: new Date(Date.now() - 400_000).toISOString(),
    last_polled_at: new Date(Date.now() - 400_000).toISOString(),
    accrued_cost_usd: 2.0,
    final_cost_usd: 2.0,
    result_reference: null,
    task_title: 'Benchmark: latency_ms <= 100',
    task_type: 'compute_experiment',
    success_criterion: { metric: 'latency_ms', operator: '<=', threshold: 100 },
    criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 145, met: false, measurable: true },
  },
];

let jobsToReturn: ComputeJob[] = stressJobs;

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    fetchComputeJobs: vi.fn(async () => jobsToReturn),
    killComputeJob: vi.fn(async () => stressJobs[0]),
    analyzeComputeJob: vi.fn(),
  };
});

import { ComputeJobsPanel } from './ComputeJobsPanel';

describe('ComputeJobsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders active + killed jobs without overflow/truncation/small-target at 375px', async () => {
    jobsToReturn = stressJobs;
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeJobsPanel projectId="proj-1" />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      },
      { waitMs: 60 },
    );
  });

  test('renders success-criterion met/unmet verdict without overflow at 1280px', async () => {
    jobsToReturn = criterionJobs;
    await expectFullViewportAt(
      1280,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeJobsPanel projectId="proj-1" />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      },
      { waitMs: 60 },
    );
  });
});
