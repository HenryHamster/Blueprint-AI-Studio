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

const commandLogJobs: ComputeJob[] = [
  {
    id: 'job-log-aaaa',
    project_id: 'proj-1',
    task_id: 'task-log-1',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-4',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 10.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 900_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 890_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 3.14,
    final_cost_usd: 3.14,
    result_reference: null,
    task_title: 'compute_experiment: benchmark accuracy on held-out eval set',
    task_type: 'compute_experiment',
    success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.93, met: true, measurable: true },
    polling_metadata: {
      command_log: {
        text: [
          'Epoch 1/5: loss=1.234 acc=0.721',
          'Epoch 2/5: loss=0.876 acc=0.803',
          'Epoch 3/5: loss=0.612 acc=0.851',
          'Epoch 4/5: loss=0.441 acc=0.882',
          'Epoch 5/5: loss=0.318 acc=0.931',
          'Eval complete. accuracy=0.93',
        ].join('\n'),
        truncated: false,
        byte_count: 148,
      },
    },
  },
];

const fusedVerdictJobs: ComputeJob[] = [
  {
    id: 'job-held',
    project_id: 'proj-1',
    task_id: 'task-held',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-2',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 900_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 890_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 1.5,
    final_cost_usd: 1.5,
    result_reference: null,
    task_title: 'Hypothesis-HELD: accuracy improves with augmentation',
    task_type: 'compute_experiment',
    experiment: 'aug-accuracy-v1',
    hypothesis: 'Data augmentation will raise accuracy above 0.9 on the held-out set.',
    success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
    criterion_verdict: { metric: 'accuracy', operator: '>=', threshold: 0.9, value: 0.94, met: true, measurable: true },
  },
  {
    id: 'job-refuted',
    project_id: 'proj-1',
    task_id: 'task-refuted',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-2',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 800_000).toISOString(),
    updated_at: new Date(Date.now() - 400_000).toISOString(),
    started_at: new Date(Date.now() - 790_000).toISOString(),
    completed_at: new Date(Date.now() - 400_000).toISOString(),
    last_polled_at: new Date(Date.now() - 400_000).toISOString(),
    accrued_cost_usd: 2.0,
    final_cost_usd: 2.0,
    result_reference: null,
    task_title: 'Hypothesis-REFUTED: latency within budget',
    task_type: 'compute_experiment',
    experiment: 'latency-budget-v1',
    hypothesis: 'The optimized inference path will keep latency under 100ms.',
    success_criterion: { metric: 'latency_ms', operator: '<=', threshold: 100 },
    criterion_verdict: { metric: 'latency_ms', operator: '<=', threshold: 100, value: 145, met: false, measurable: true },
  },
  {
    id: 'job-inconclusive',
    project_id: 'proj-1',
    task_id: 'task-inconclusive',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'e2-standard-2',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 700_000).toISOString(),
    updated_at: new Date(Date.now() - 500_000).toISOString(),
    started_at: new Date(Date.now() - 690_000).toISOString(),
    completed_at: new Date(Date.now() - 500_000).toISOString(),
    last_polled_at: new Date(Date.now() - 500_000).toISOString(),
    accrued_cost_usd: 1.0,
    final_cost_usd: 1.0,
    result_reference: null,
    task_title: 'Hypothesis-INCONCLUSIVE: f1 improvement',
    task_type: 'compute_experiment',
    experiment: 'f1-improvement-v1',
    hypothesis: 'The new loss weighting scheme will improve F1 above 0.8.',
    success_criterion: { metric: 'f1', operator: '>=', threshold: 0.8 },
    criterion_verdict: { metric: 'f1', operator: '>=', threshold: 0.8, value: null, met: false, measurable: false },
  },
];

const armsJobs: ComputeJob[] = [
  {
    id: 'job-arms-vp',
    project_id: 'proj-1',
    task_id: 'task-arms-vp',
    backend: 'gcp_compute_engine',
    external_compute_id: null,
    machine_type: 'n1-standard-4',
    status: 'SUCCEEDED',
    terminal_reason: 'experiment completed',
    outcome: 'SUCCESS',
    spend_cap_usd: 5.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 900_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 890_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 2.5,
    final_cost_usd: 2.5,
    result_reference: null,
    task_title: 'A/B arm experiment: learning rate sweep',
    task_type: 'compute_experiment',
    experiment: 'lr-ab-sweep',
    success_criterion: { metric: 'val_acc', operator: '>=', threshold: 0.85 },
    criterion_verdict: {
      metric: 'val_acc',
      operator: '>=',
      threshold: 0.85,
      value: 0.88,
      met: true,
      measurable: true,
    },
    polling_metadata: {
      result_metrics: {
        val_acc: 0.88,
        arms: {
          'arm-lr-0.01': { val_acc: 0.86, val_loss: 0.32, epochs: 50 },
          'arm-lr-0.001': { val_acc: 0.88, val_loss: 0.28, epochs: 50 },
          'arm-lr-0.0001': { val_acc: 0.82, val_loss: 0.41, epochs: 50 },
        },
      },
    },
  },
];

const progressSeriesJobs: ComputeJob[] = [
  {
    id: 'job-progress',
    project_id: 'proj-1',
    task_id: 'task-progress',
    backend: 'local-gpu',
    external_compute_id: null,
    machine_type: 'a3-highgpu-1g',
    status: 'TIMED_OUT',
    terminal_reason: 'no progress detected over 30m window',
    outcome: 'TIMED_OUT',
    terminal_kind: 'no_progress',
    accelerator_type: 'H100',
    accelerator_count: 1,
    spend_cap_usd: 10.0,
    wall_clock_deadline: null,
    created_at: new Date(Date.now() - 3_600_000).toISOString(),
    updated_at: new Date(Date.now() - 300_000).toISOString(),
    started_at: new Date(Date.now() - 3_590_000).toISOString(),
    completed_at: new Date(Date.now() - 300_000).toISOString(),
    last_polled_at: new Date(Date.now() - 300_000).toISOString(),
    accrued_cost_usd: 2.1,
    final_cost_usd: 2.1,
    result_reference: null,
    task_title: 'compute_experiment: RL training run — reward plateau detected',
    task_type: 'compute_experiment',
    experiment: 'rl-reward-v1',
    progress_series: [
      { t: new Date(Date.now() - 3_000_000).toISOString(), name: 'reward', value: 0.12 },
      { t: new Date(Date.now() - 2_700_000).toISOString(), name: 'reward', value: 0.18 },
      { t: new Date(Date.now() - 2_400_000).toISOString(), name: 'reward', value: 0.21 },
      { t: new Date(Date.now() - 2_100_000).toISOString(), name: 'reward', value: 0.22 },
      { t: new Date(Date.now() - 1_800_000).toISOString(), name: 'reward', value: 0.22 },
      { t: new Date(Date.now() - 1_500_000).toISOString(), name: 'reward', value: 0.21 },
      { t: new Date(Date.now() - 1_200_000).toISOString(), name: 'reward', value: 0.22 },
      { t: new Date(Date.now() - 900_000).toISOString(), name: 'reward', value: 0.22 },
    ],
  },
  {
    id: 'job-progress-active',
    project_id: 'proj-1',
    task_id: 'task-progress-active',
    backend: 'local-gpu',
    external_compute_id: null,
    machine_type: 'a3-highgpu-1g',
    status: 'RUNNING',
    terminal_reason: null,
    outcome: null,
    accelerator_type: 'H100',
    accelerator_count: 1,
    spend_cap_usd: 20.0,
    wall_clock_deadline: new Date(Date.now() + 4 * 3_600_000).toISOString(),
    created_at: new Date(Date.now() - 1_800_000).toISOString(),
    updated_at: new Date(Date.now() - 30_000).toISOString(),
    started_at: new Date(Date.now() - 1_790_000).toISOString(),
    completed_at: null,
    last_polled_at: new Date(Date.now() - 30_000).toISOString(),
    accrued_cost_usd: 1.8,
    final_cost_usd: null,
    result_reference: null,
    task_title: 'compute_experiment: RL training — actively improving',
    task_type: 'compute_experiment',
    experiment: 'rl-reward-v2',
    progress_series: [
      { t: new Date(Date.now() - 1_500_000).toISOString(), name: 'reward', value: 0.05 },
      { t: new Date(Date.now() - 1_200_000).toISOString(), name: 'reward', value: 0.12 },
      { t: new Date(Date.now() - 900_000).toISOString(), name: 'reward', value: 0.21 },
      { t: new Date(Date.now() - 600_000).toISOString(), name: 'reward', value: 0.35 },
      { t: new Date(Date.now() - 300_000).toISOString(), name: 'reward', value: 0.49 },
    ],
  },
];

let jobsToReturn: ComputeJob[] = stressJobs;

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    fetchComputeJobs: vi.fn(async () => jobsToReturn),
    fetchComputeJobsPaged: vi.fn(async () => ({ jobs: jobsToReturn, hasMore: false })),
    killComputeJob: vi.fn(async () => stressJobs[0]),
    analyzeComputeJob: vi.fn(),
    resolveArtifactDownloadUrl: vi.fn(async () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='),
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

  test('renders command log for a terminal job at 1280px', async () => {
    jobsToReturn = commandLogJobs;
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

  test('renders HELD/REFUTED/INCONCLUSIVE fused verdict cards at 1280px', async () => {
    jobsToReturn = fusedVerdictJobs;
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
        const text = host.textContent ?? '';
        if (!text.includes('HELD')) throw new Error('HELD verdict badge not rendered');
        if (!text.includes('REFUTED')) throw new Error('REFUTED verdict badge not rendered');
        if (!text.includes('INCONCLUSIVE')) throw new Error('INCONCLUSIVE verdict badge not rendered');
        const held = host.querySelector('[data-testid="verdict-label"]');
        if (!held) throw new Error('verdict-label testid not found');
      },
      { waitMs: 60 },
    );
  });

  test('renders terminal job with per-arm breakdown without overflow at 1024px', async () => {
    jobsToReturn = armsJobs;
    await expectFullViewportAt(
      1024,
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
        const text = host.textContent ?? '';
        if (!text.includes('arm-lr-0.01')) throw new Error('arms breakdown not rendered');
        if (text.includes('[object Object]')) throw new Error('[object Object] still rendered');
      },
      { waitMs: 60 },
    );
  });

  test('renders re-run button disabled when job has no source task at 1024px', async () => {
    const jobWithoutTaskId: ComputeJob = {
      id: 'job-no-task',
      project_id: 'proj-1',
      task_id: null,
      backend: 'gcp_compute_engine',
      external_compute_id: null,
      machine_type: 'e2-standard-2',
      status: 'SUCCEEDED',
      terminal_reason: 'experiment completed',
      outcome: 'SUCCESS',
      spend_cap_usd: 5.0,
      wall_clock_deadline: null,
      created_at: new Date(Date.now() - 900_000).toISOString(),
      updated_at: new Date(Date.now() - 300_000).toISOString(),
      started_at: new Date(Date.now() - 890_000).toISOString(),
      completed_at: new Date(Date.now() - 300_000).toISOString(),
      last_polled_at: new Date(Date.now() - 300_000).toISOString(),
      accrued_cost_usd: 1.5,
      final_cost_usd: 1.5,
      result_reference: null,
      task_title: 'compute_experiment: no source task',
      task_type: 'compute_experiment',
      accelerator_type: null,
      accelerator_count: null,
    };
    jobsToReturn = [jobWithoutTaskId];
    await expectFullViewportAt(
      1024,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeJobsPanel projectId="proj-1" onDuplicateTask={async () => {}} />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
        const text = host.textContent ?? '';
        if (!text.includes('no source task')) throw new Error('job title not rendered');
        if (!text.includes('Run analysis')) throw new Error('Run analysis button not rendered');
        if (!text.includes('Re-run')) throw new Error('Re-run button not rendered');
        const allBtns = Array.from(host.querySelectorAll('button'));
        const rerunBtn = allBtns.find(btn => (btn.textContent ?? '').includes('Re-run'));
        if (!rerunBtn) throw new Error('Re-run button element not found');
        if (!rerunBtn.disabled) throw new Error('Re-run button should be disabled');
      },
      { waitMs: 60 },
    );
  });

  test('compact table view renders sortable columns at 1200px', async () => {
    jobsToReturn = criterionJobs;
    await expectFullViewportAt(
      1200,
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
        // Switch to compact table view
        const toggleBtn = host.querySelector('[data-testid="compute-view-mode-toggle"]') as HTMLButtonElement | null;
        if (!toggleBtn) throw new Error('View mode toggle button not found');
        await act(async () => { toggleBtn.click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 20)); });
        // Table should be rendered
        const table = host.querySelector('[data-testid="compact-jobs-table"]');
        if (!table) throw new Error('compact-jobs-table not rendered after toggle');
        // Sortable column headers should be present
        const nameHeader = host.querySelector('[data-testid="jobs-table-sort-name"]');
        if (!nameHeader) throw new Error('Name sort header not found');
        const costHeader = host.querySelector('[data-testid="jobs-table-sort-cost"]');
        if (!costHeader) throw new Error('Cost sort header not found');
        // Criterion chips should render for both jobs
        const chips = host.querySelectorAll('[data-testid="compact-table-criterion-chip"]');
        if (chips.length < 2) throw new Error(`Expected at least 2 criterion chips, got ${chips.length}`);
        // Verify met/unmet chips are present
        const text = host.textContent ?? '';
        if (!text.includes('met')) throw new Error('met criterion chip not rendered');
        if (!text.includes('unmet')) throw new Error('unmet criterion chip not rendered');
      },
      { waitMs: 60 },
    );
  });

  test('renders artifact preview gallery in job card at 1100px', async () => {
    jobsToReturn = [
      {
        id: 'job-gallery',
        project_id: 'proj-1',
        task_id: 'task-gallery',
        backend: 'local-gpu',
        external_compute_id: null,
        machine_type: 'a3-highgpu-1g',
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
        accrued_cost_usd: 1.2,
        final_cost_usd: 1.2,
        result_reference: null,
        task_title: 'compute_experiment: training loss curves',
        task_type: 'compute_experiment',
        accelerator_type: null,
        accelerator_count: null,
        viewable_artifacts: [
          { storage_uri: 'gs://bucket/jobs/job-gallery/loss.png', filename: 'loss.png', kind: 'image' as const, content_type: 'image/png', size_bytes: 8192 },
          { storage_uri: 'gs://bucket/jobs/job-gallery/acc.png', filename: 'acc.png', kind: 'image' as const, content_type: 'image/png', size_bytes: 7680 },
        ],
      },
    ];
    await expectFullViewportAt(
      1100,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<ComputeJobsPanel projectId="proj-1" />);
        });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 100)); });
        const gallery = host.querySelector('[data-testid="compute-artifact-gallery"]');
        if (!gallery) throw new Error('compute-artifact-gallery not rendered');
        const toggle = host.querySelector('[data-testid="compute-artifact-gallery-toggle"]') as HTMLButtonElement | null;
        if (!toggle) throw new Error('compute-artifact-gallery-toggle not rendered');
        await act(async () => { toggle.click(); });
        const slots = host.querySelectorAll('[data-testid="compute-viewable-artifact"]');
        if (slots.length < 2) throw new Error(`Expected 2 gallery slots, got ${slots.length}`);
      },
      { waitMs: 120 },
    );
  });

  test('renders progress series sparkline and no-progress stall tag at 1280px', async () => {
    jobsToReturn = progressSeriesJobs;
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
        const text = host.textContent ?? '';
        if (!text.includes('reward')) throw new Error('progress metric name not rendered');
        if (!text.includes('no-progress cut')) throw new Error('no-progress cut tag not rendered for stalled job');
        const seriesBlocks = host.querySelectorAll('[data-testid="compute-progress-series"]');
        if (seriesBlocks.length < 2) throw new Error(`Expected 2 progress-series blocks, got ${seriesBlocks.length}`);
        const sparklines = host.querySelectorAll('polyline');
        if (sparklines.length < 2) throw new Error(`Expected 2 sparkline polylines, got ${sparklines.length}`);
      },
      { waitMs: 60 },
    );
  });
});
