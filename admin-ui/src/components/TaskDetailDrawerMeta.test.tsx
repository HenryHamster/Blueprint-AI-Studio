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

  it('renders the coverage counts when a passed suite reports coverage', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        coverage: { passed: 100, skipped: 0, total: 100 },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Coverage')).toBeInTheDocument();
    expect(screen.getByText('100 passed · 0 skipped · 100 total')).toBeInTheDocument();
    expect(screen.queryByText(/most of its tests were skipped/)).toBeNull();
  });

  it('flags a heavily-skipped pass with an amber mostly-skipped note', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        coverage: { passed: 5, skipped: 1500, total: 1505 },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('5 passed · 1500 skipped · 1505 total')).toBeInTheDocument();
    expect(screen.getByText(/most of its tests were skipped/)).toBeInTheDocument();
    // Still a PASS verdict — visibility only, not a block.
    expect(screen.getByText('Passed')).toBeInTheDocument();
  });

  it('renders no coverage row for a legacy artifact without coverage', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({ detected: true, passed: true, tests: [], output: '' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merge Test Gate')).toBeInTheDocument();
    expect(screen.queryByText('Coverage')).toBeNull();
    expect(screen.queryByText(/most of its tests were skipped/)).toBeNull();
  });

  it('surfaces a prominent disabled-tests signal when the PR newly disabled tests', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        disabledTests: {
          count: 2,
          markers: [
            { file: 'src/foo.test.ts', marker: 'it.skip', line: "it.skip('x')" },
            { file: 'test_bar.py', marker: '@pytest.mark.skip', line: '@pytest.mark.skip' },
          ],
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Disabled tests')).toBeInTheDocument();
    expect(screen.getByText(/This PR disables 2 tests/)).toBeInTheDocument();
    expect(screen.getByText('src/foo.test.ts')).toBeInTheDocument();
    // Visibility only — the suite still reads as Passed, not Blocked.
    expect(screen.getByText('Passed')).toBeInTheDocument();
  });

  it('renders no disabled-tests row for a legacy artifact without the field', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({ detected: true, passed: true, tests: [], output: '' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merge Test Gate')).toBeInTheDocument();
    expect(screen.queryByText('Disabled tests')).toBeNull();
  });

  it('blocks (not Passed) when a documented command failed despite a passing unit suite', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        documentedCommands: {
          ran: true, outcome: 'fail', passed: false, could_not_run: false,
          output: 'lint: 3 problems', label: 'npm run lint',
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.queryByText('Passed')).toBeNull();
    expect(screen.getByText('Documented command')).toBeInTheDocument();
    expect(screen.getByText('npm run lint: failed')).toBeInTheDocument();
    expect(screen.getByText('Command Output')).toBeInTheDocument();
    expect(screen.getByText(/3 problems/)).toBeInTheDocument();
  });

  it('renders a passing documented-command row', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        documentedCommands: {
          ran: true, outcome: 'pass', passed: true, could_not_run: false,
          output: '', label: 'make smoke',
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Passed')).toBeInTheDocument();
    expect(screen.getByText('make smoke: passed')).toBeInTheDocument();
  });

  it('renders a could-not-run documented-command row without blocking', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        documentedCommands: {
          ran: true, outcome: 'could_not_run', passed: false, could_not_run: true,
          output: '', label: 'make smoke',
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Passed')).toBeInTheDocument();
    expect(screen.getByText('make smoke: could not run')).toBeInTheDocument();
  });

  it('blocks on a partial-setup failure', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: 'npm ci failed for the TS runner',
        partialSetupFailed: true,
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.getByText(/only partially verified/)).toBeInTheDocument();
  });

  it('blocks and attributes a failed declared build', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        probe: {
          ran: true, ecosystem: 'node', outcome: 'fail', passed: false, could_not_run: false,
          declared: true, output: 'build exited 1', label: 'npm run build',
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.getByText('(declared build)')).toBeInTheDocument();
    expect(screen.getByText(/declared build command failed/)).toBeInTheDocument();
  });

  it('attributes a generic-check probe distinctly from a declared build', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        probe: { ran: true, ecosystem: 'node', outcome: 'pass', passed: true, could_not_run: false, declared: false },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('node: passed')).toBeInTheDocument();
    expect(screen.getByText(/generic check/)).toBeInTheDocument();
  });

  it('warns when a declared build passed but wrote no artifact (possible no-op build)', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        probe: {
          ran: true, ecosystem: 'node', outcome: 'pass', passed: true, could_not_run: false,
          declared: true, wrote_no_artifact: true, label: 'npm run build',
        },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText(/possible no-op build/)).toBeInTheDocument();
  });

  it('shows the stale-command recovery and the replacement directories that ran', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        recovered: true,
        recovered_directories: [
          { path: 'packages/api', type: 'node', test_cmd: 'npm test' },
          { path: 'svc', type: 'python', test_cmd: 'pytest' },
        ],
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Recovered suite')).toBeInTheDocument();
    expect(screen.getByText(/stored test command was stale/)).toBeInTheDocument();
    expect(screen.getByText('packages/api')).toBeInTheDocument();
    expect(screen.getByText(/npm test/)).toBeInTheDocument();
    expect(screen.getByText('svc')).toBeInTheDocument();
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
    expect(screen.getByText('Pass')).toBeInTheDocument();
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
    const badge = screen.getByText('Approve');
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
    const badge = screen.getByText('Request Changes');
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

describe('DetailsTab — Structural Gating', () => {
  afterEach(() => cleanup());

  it('renders foundation convergence reason when task is serialized behind a duplicate', () => {
    const foundationTask = makeTask({ id: 'foundation-1', title: 'Original API implementation' });
    const task = makeTask({
      id: 'duplicate-task',
      context: {
        _foundation_converged: {
          foundation_id: 'foundation-1',
          reason: 'shared finding origin',
          shared_files: ['backend/api.py', 'backend/models.py'],
        },
      },
    });
    render(<DetailsTab task={task} tasks={[task, foundationTask]} />);
    expect(screen.getByText('Structural Serialization')).toBeInTheDocument();
    expect(screen.getByText('Serialized behind duplicate foundation')).toBeInTheDocument();
    expect(screen.getByText('shared finding origin')).toBeInTheDocument();
    expect(screen.getByText('backend/api.py')).toBeInTheDocument();
    expect(screen.getByText('backend/models.py')).toBeInTheDocument();
  });

  it('links to foundation task and shows title when available', () => {
    const foundationTask = makeTask({ id: 'foundation-1', title: 'Original API implementation' });
    const task = makeTask({
      context: {
        _foundation_converged: {
          foundation_id: 'foundation-1',
          reason: 'near-identical file sets',
          shared_files: ['file.ts'],
        },
      },
    });
    const mockNavigate = vi.fn();
    render(<DetailsTab task={task} tasks={[task, foundationTask]} onNavigateToTask={mockNavigate} />);
    const link = screen.getByText('Original API implementation');
    expect(link).toBeInTheDocument();
    expect(link.closest('button')).toBeInTheDocument();
  });

  it('renders blocked need when task carries a machine-resolvable need', () => {
    const blockedTask = makeTask({
      status: 'BLOCKED',
      context: {
        blocker: {
          need: {
            kind: 'task',
            ref: 'prerequisite-123',
            label: 'Foundation task must complete first',
          },
        },
      },
    });
    const prereqTask = makeTask({ id: 'prerequisite-123', title: 'Foundation task' });
    render(<DetailsTab task={blockedTask} tasks={[blockedTask, prereqTask]} />);
    expect(screen.getByText('Blocker')).toBeInTheDocument();
    expect(screen.getByText('Waiting for task')).toBeInTheDocument();
    expect(screen.getByText('Foundation task must complete first')).toBeInTheDocument();
  });

  it('shows blocked need ref as text when prerequisite task not found', () => {
    const task = makeTask({
      status: 'BLOCKED',
      context: {
        blocker: {
          need: {
            kind: 'capability',
            ref: 'capability:database-migration',
            label: 'Database migration capability',
          },
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Waiting for capability')).toBeInTheDocument();
    expect(screen.getByText('capability:database-migration')).toBeInTheDocument();
    expect(screen.getByText('Database migration capability')).toBeInTheDocument();
  });
});
