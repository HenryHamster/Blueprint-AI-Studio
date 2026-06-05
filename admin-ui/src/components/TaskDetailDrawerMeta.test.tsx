import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { DetailsTab } from './TaskDetailDrawerMeta';
import type { Task } from '../types/task';
import { fetchComputeJobs } from '../api';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', () => ({
  authGet: vi.fn(async () => new Response(JSON.stringify({ stats: {} }), { status: 200 })),
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ task_id: 'task-1', fix_loop_history: {}, summary: {} }), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  fetchComputeJobs: vi.fn(async () => []),
  fetchKnowledgeDocument: vi.fn(async () => ({ id: 'doc-1', body: '# Full body\n\nDetailed markdown.' })),
  fetchTaskSerialConsole: vi.fn(async () => ({ serial_log: '', source: 'cached', vm_status: 'TERMINATED', vm_instance_id: 'test' })),
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-verify-1',
    title: 'Verify tests on PR head',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'verify_tests',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'ORCHESTRATOR',
    requires_approval: false,
    ...overrides,
  };
}

function mergeTestArtifact(verdict: Record<string, unknown>, iteration = 1) {
  return { artifact_type: 'MERGE_TEST_RESULT', logs: JSON.stringify(verdict), iteration };
}

describe('DetailsTab — Merge Test Gate', () => {
  afterEach(() => cleanup());

  it('renders a Passed verdict when the detected suite passed', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({ detected: true, passed: true, tests: [], output: '' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merge Test Gate')).toBeInTheDocument();
    expect(screen.getByText('Passed')).toBeInTheDocument();
    expect(screen.getByText(/served as the merge gate/)).toBeInTheDocument();
  });

  it('renders a Failed verdict and surfaces the test output', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: false,
        tests: [{ name: 'jest', status: 'fail', message: 'test failures detected' }],
        output: 'FAIL src/foo.test.ts — expected 2 received 3',
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('Test Output')).toBeInTheDocument();
    expect(screen.getByText(/expected 2 received 3/)).toBeInTheDocument();
  });

  it('renders an undetected verdict when no suite was detected', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({ detected: false, passed: false, tests: [], output: 'No test runner detected' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('No suite detected')).toBeInTheDocument();
    expect(screen.getByText(/proceeding unverified/)).toBeInTheDocument();
  });

  it('uses the latest iteration when multiple verdicts exist', () => {
    const task = makeTask({
      artifacts: [
        mergeTestArtifact({ detected: true, passed: false, tests: [], output: 'old failure' }, 1),
        mergeTestArtifact({ detected: true, passed: true, tests: [], output: '' }, 2),
      ],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Passed')).toBeInTheDocument();
    expect(screen.queryByText('Failed')).toBeNull();
  });

  it('renders nothing when there is no MERGE_TEST_RESULT artifact', () => {
    const task = makeTask({ artifacts: [{ artifact_type: 'GIT_COMMIT', commit_hash: 'abc', iteration: 1 }] });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Merge Test Gate')).toBeNull();
  });

  it('renders nothing when the verdict logs are not valid JSON', () => {
    const task = makeTask({ artifacts: [{ artifact_type: 'MERGE_TEST_RESULT', logs: 'not-json{', iteration: 1 }] });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Merge Test Gate')).toBeNull();
  });

  it('renders an UNVERIFIED verdict with the mapped reason when no artifact exists', () => {
    const task = makeTask({ unverified_merge_reason: 'no_detectable_test_command' });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merge Test Gate')).toBeInTheDocument();
    expect(screen.getByText('Merged unverified')).toBeInTheDocument();
    expect(screen.getByText(/no runnable test command could be detected/)).toBeInTheDocument();
  });

  it('humanizes an unmapped reason key instead of showing a raw token', () => {
    const task = makeTask({ unverified_merge_reason: 'some_future_reason' });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merged unverified')).toBeInTheDocument();
    // Forward-compat: an unknown key is de-tokenized, never shown raw.
    expect(screen.getByText(/some future reason/)).toBeInTheDocument();
    expect(screen.queryByText(/some_future_reason/)).toBeNull();
  });

  it('renders the acceptance-probe outcome alongside the suite verdict', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: 'No test runner detected',
        probe: { ran: true, ecosystem: 'node', passed: true, could_not_run: false },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Acceptance probe')).toBeInTheDocument();
    expect(screen.getByText('node: passed')).toBeInTheDocument();
  });

  it('renders a could-not-run acceptance probe on a fall-open', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        probe: { ran: true, ecosystem: 'python', passed: false, could_not_run: true },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('python: could not run')).toBeInTheDocument();
  });

  it('maps the probe_passed reason to an accurate, non-misleading label', () => {
    const task = makeTask({ unverified_merge_reason: 'probe_passed' });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merged unverified')).toBeInTheDocument();
    expect(screen.getByText(/not real test verification/)).toBeInTheDocument();
  });

  it('reason label takes precedence when both an undetected artifact and a reason are present', () => {
    const task = makeTask({
      unverified_merge_reason: 'kill_switch_off',
      artifacts: [mergeTestArtifact({ detected: false, passed: false, tests: [], output: 'No test runner detected' })],
    });
    render(<DetailsTab task={task} />);
    // The reason-driven UNVERIFIED verdict wins over the artifact's 'undetected'.
    expect(screen.getByText('Merged unverified')).toBeInTheDocument();
    expect(screen.queryByText('No suite detected')).toBeNull();
    expect(screen.getByText(/kill-switch off/)).toBeInTheDocument();
    // Captured test output from the artifact is still surfaced.
    expect(screen.getByText('Test Output')).toBeInTheDocument();
    expect(screen.getByText(/No test runner detected/)).toBeInTheDocument();
  });

  it('renders the normalized evaluation summary alongside legacy task details', () => {
    const task = makeTask({
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'pass',
        evidence: [
          { label: 'Tests', value: '23 passed, 0 failed' },
          { label: 'Gate', value: 'verified on PR head' },
        ],
        highlights: [
          { label: 'Top', value: 'full suite green' },
        ],
      },
      artifacts: [mergeTestArtifact({ detected: true, passed: true, tests: [], output: '' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Evaluation Summary')).toBeInTheDocument();
    expect(screen.getByText('pass')).toBeInTheDocument();
    expect(screen.getByText(/verified on PR head/)).toBeInTheDocument();
    expect(screen.getByText('Merge Test Gate')).toBeInTheDocument();
  });

  it('colors an approve panel verdict as a positive (emerald) badge', () => {
    const task = makeTask({
      task_type: 'panel_review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'approve',
        evidence: [{ label: 'Decision', value: 'APPROVE' }],
        highlights: [{ label: 'Correctness', value: 'pass' }],
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('approve');
    expect(badge.className).toContain('emerald');
    expect(badge.className).not.toContain('cyan');
  });

  it('colors a request_changes panel verdict as a blocking (red) badge', () => {
    const task = makeTask({
      task_type: 'panel_review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'request_changes',
        evidence: [{ label: 'Decision', value: 'REQUEST_CHANGES' }],
        highlights: [{ label: 'Correctness', value: 'fail' }],
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('request_changes');
    expect(badge.className).toContain('red');
    expect(badge.className).not.toContain('cyan');
  });

  it('renders the terminal compute result reference when present', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce([
      {
        id: 'job-1',
        project_id: 'proj-1',
        task_id: 'task-verify-1',
        backend: 'gcp_compute_engine',
        external_compute_id: 'compute-job-vm-1',
        machine_type: 'e2-standard-2',
        status: 'SUCCEEDED',
        terminal_reason: 'compute job finished',
        outcome: 'SUCCESS',
        spend_cap_usd: 4,
        wall_clock_deadline: '2026-01-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:10:00Z',
        started_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:10:00Z',
        last_polled_at: '2026-01-01T00:10:00Z',
        accrued_cost_usd: 1.5,
        final_cost_usd: 1.5,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
        task_title: 'compute_experiment: run a budgeted job',
        task_type: 'compute_experiment',
      },
    ]);
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    expect(await screen.findByRole('link', { name: /https:\/\/storage\.example\.com\/jobs\/job-1\/result\.json/i })).toHaveAttribute(
      'href',
      'https://storage.example.com/jobs/job-1/result.json',
    );
  });

  it('renders structured panel review verdict details without falling back to raw plan JSON', () => {
    const task = makeTask({
      task_type: 'panel_review',
      plan_output: {
        overall_verdict: 'REQUEST_CHANGES',
        summary: 'Two blocking findings remain.',
        investigated_files: [
          'agent/src/handlers/panel-review.ts',
          'admin-ui/src/components/TaskDetailDrawerMeta.tsx',
        ],
        lenses: {
          correctness: {
            verdict: 'REQUEST_CHANGES',
            summary: 'A caller compatibility issue remains.',
            issues: [
              {
                severity: 'major',
                description: 'Caller still expects the old payload shape.',
                file: 'agent/src/handlers/panel-review.ts',
                line: 120,
              },
            ],
          },
          security: {
            verdict: 'APPROVE',
            summary: 'No security regressions were found.',
            issues: [],
          },
          code_quality: {
            verdict: 'APPROVE',
            summary: 'The workflow stays inside the existing review infrastructure.',
            issues: [],
          },
          test_coverage: {
            verdict: 'APPROVE',
            summary: 'Targeted tests cover the new workflow.',
            issues: [],
          },
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Panel Review Verdict')).toBeInTheDocument();
    expect(screen.getAllByText('Request Changes').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Two blocking findings remain/)).toBeInTheDocument();
    expect(screen.getByText('Correctness')).toBeInTheDocument();
    expect(screen.getByText('agent/src/handlers/panel-review.ts:120')).toBeInTheDocument();
    expect(screen.queryByText('Plan Output')).toBeNull();
  });
});

describe('DetailsTab — Knowledge Document', () => {
  afterEach(() => cleanup());

  function knowledgeTask(): Task {
    return makeTask({
      task_type: 'design',
      project_id: 'proj-1' as unknown as Task['project_id'],
      plan_output: {
        knowledge_document: {
          id: 'doc-1',
          title: 'Auth subsystem design',
          summary: 'A codebase-internal design of the auth subsystem.',
          citation_count: 3,
          forward_quality_verdict: { passed: true, reason: 'coherent and grounded' },
        },
      },
    });
  }

  it('surfaces the produced document title, verdict and citation count', () => {
    render(<DetailsTab task={knowledgeTask()} />);
    expect(screen.getByText('Knowledge Document')).toBeInTheDocument();
    expect(screen.getByText('Auth subsystem design')).toBeInTheDocument();
    expect(screen.getByText(/Forward quality: passed/)).toBeInTheDocument();
    expect(screen.getByText('3 citations')).toBeInTheDocument();
    // The redundant raw Plan Output dump is suppressed when the mirror is present.
    expect(screen.queryByText('Plan Output')).toBeNull();
  });

  it('fetches and renders the full document body on demand', async () => {
    const { fetchKnowledgeDocument } = await import('../api');
    render(<DetailsTab task={knowledgeTask()} />);
    screen.getByText('View document').click();
    expect(fetchKnowledgeDocument).toHaveBeenCalledWith('proj-1', 'doc-1');
    expect(await screen.findByText(/Detailed markdown/)).toBeInTheDocument();
  });
});
