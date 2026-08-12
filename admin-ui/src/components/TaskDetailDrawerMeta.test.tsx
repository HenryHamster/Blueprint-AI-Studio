import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { DetailsTab } from './TaskDetailDrawerMeta';
import type { Task } from '../types/task';
import { authGet, fetchComputeJobs, resolveArtifactDownloadUrl, resolveViewportFailureDownloadUrl, resolveViewportShotDownloadUrl } from '../api';
import type { ComputeJob, ComputeJobsListResponse } from '../api';
import { formatAbsolute } from '../utils/time';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

// This file tests drawer data flow, not the separately-covered lazy Markdown renderer.
// Keep assertions independent of dynamic chunk scheduling under the full Vitest pool.
vi.mock('./MarkdownContent', () => ({ MarkdownContent: ({ content }: { content: string }) => <div>{content}</div> }));


vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(async () => new Response(JSON.stringify({ stats: {} }), { status: 200 })),
    apiFetch: vi.fn(async () => new Response(JSON.stringify({ task_id: 'task-1', fix_loop_history: {}, summary: {} }), { status: 200 })),
    API_BASE_URL: 'http://localhost:8000',
    fetchComputeJobs: vi.fn(async () => ({ jobs: [], hasMore: false, summary: { total_jobs: 0, active_jobs: 0, terminal_jobs: 0, total_spend_usd: 0, outcome_breakdown: {}, criterion_measured: 0, criterion_met: 0 }, limit: 50, offset: 0 })),
    fetchKnowledgeDocument: vi.fn(async () => ({ id: 'doc-1', body: '# Full body\n\nDetailed markdown.' })),
    resolveArtifactDownloadUrl: vi.fn(async () => 'https://signed.example.com/tasks/task-1/result.json'),
    resolveViewportFailureDownloadUrl: vi.fn(async () => 'https://signed.example.com/viewport/screenshot.png'),
    resolveViewportShotDownloadUrl: vi.fn(async () => 'https://signed.example.com/viewport-shots/shot.png'),
    fetchTaskSerialConsole: vi.fn(async () => ({ serial_log: '', source: 'cached', vm_status: 'TERMINATED', vm_instance_id: 'test' })),
  });
});

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

describe('DetailsTab — sideboard context', () => {
  afterEach(() => {
    vi.mocked(authGet).mockResolvedValue(new Response(JSON.stringify({ stats: {} }), { status: 200 }));
    cleanup();
  });

  it('prefers the lifetime attempt ordinal after a retry-budget reset', () => {
    render(
      <DetailsTab
        task={makeTask({ attempt_count: 0, attempt_number: 2 })}
      />,
    );

    expect(screen.getByText('Attempt').parentElement).toHaveTextContent('2');
  });

  it('renders bounded task sideboard context', () => {
    const onNavigateToTask = vi.fn();
    render(
      <DetailsTab
        task={makeTask({
          id: 'task-sideboard',
          project_id: 'proj-1',
          sideboard_context: {
            project: { id: 'proj-1', name: 'Blueprint', repo: 'HenryHamster/BlueprintAIStudio' },
            purpose: 'Task sideboard integration',
            strategic: {
              direction: {
                id: 'd-active',
                text: 'Keep active work tied to the operator sideboard.',
                priority: 'high',
                status: 'active',
              },
              cycle: { id: 'sc-1', phase: 'strategic_review', outcome: 'phase2_running' },
              parent_strategic_task_id: 'sc-1',
            },
            pipeline: {
              parent_task_id: 'parent-task-123456',
              child_count: 2,
              dependency_count: 1,
              blocked_reason: 'Waiting for reviewer feedback.',
            },
            artifacts: {
              total: 1,
              by_type: { PULL_REQUEST: 1 },
              primary_pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
              primary_commit: 'abcdef1234567890',
            },
            messages: {
              total_recent: 3,
              items: [
                {
                  id: 'msg-1',
                  project_id: 'proj-1',
                  message_type: 'NOTE',
                  from_type: 'agent',
                  from_task_id: 'task-sideboard',
                  content: 'Recent task update',
                  context: {},
                  status: 'open',
                  resolution: null,
                  resolved_at: null,
                  created_at: '2026-06-29T12:00:00Z',
                },
                {
                  id: 'msg-2',
                  project_id: 'proj-1',
                  message_type: 'STATUS',
                  from_type: 'agent',
                  from_task_id: 'task-sideboard',
                  content: 'Second bounded task update',
                  context: {},
                  status: 'open',
                  resolution: null,
                  resolved_at: null,
                  created_at: '2026-06-29T12:02:00Z',
                },
              ],
            },
            links: [
              { id: 'pipeline', label: 'Pipeline', target: { surface: 'pipeline', id: 'task-sideboard', task_id: 'task-sideboard' } },
              { id: 'linked-task', label: 'Linked task', target: { surface: 'task', id: 'legacy-linked-task-id', task_id: 'linked-task-abcdef' } },
              { id: 'direction', label: 'Direction', target: { surface: 'project', section: 'directions', id: 'd-active', direction_id: 'd-active' } },
              { id: 'pull_request', label: 'PR', target: { surface: 'external', id: 'legacy-pr-url', url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/42' } },
            ],
          },
        })}
        onNavigateToTask={onNavigateToTask}
      />,
    );

    expect(screen.getByText('Context')).toBeInTheDocument();
    expect(screen.getByText('HenryHamster/BlueprintAIStudio')).toBeInTheDocument();
    expect(screen.getByText('Task sideboard integration')).toBeInTheDocument();
    expect(screen.getByText('Keep active work tied to the operator sideboard.')).toBeInTheDocument();
    expect(screen.getByText('sc-1')).toBeInTheDocument();
    expect(screen.getByText('strategic review')).toBeInTheDocument();
    expect(screen.getByText('phase2_running')).toBeInTheDocument();
    const dependenciesStat = screen.getByText('Dependencies').parentElement;
    const childrenStat = screen.getByText('Children').parentElement;
    expect(dependenciesStat).toHaveTextContent('1');
    expect(childrenStat).toHaveTextContent('2');
    expect(screen.getByText('Waiting for reviewer feedback.')).toBeInTheDocument();
    expect(screen.getByText('pull request 1')).toBeInTheDocument();
    expect(screen.getByText('abcdef123456')).toBeInTheDocument();
    expect(screen.getByText('Recent task update')).toBeInTheDocument();
    expect(screen.getByText('Second bounded task update')).toBeInTheDocument();
    expect(screen.getByText('+1 more recent message')).toBeInTheDocument();
    expect(screen.getAllByText('task-sideboard').length).toBeGreaterThan(0);
    expect(screen.getByText('linked-task-abcdef')).toBeInTheDocument();
    expect(screen.getByText('direction d-active')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PR github.com/HenryHamster/BlueprintAIStudio/pull/42' })).toHaveAttribute(
      'href',
      'https://github.com/HenryHamster/BlueprintAIStudio/pull/42',
    );

    fireEvent.click(screen.getByRole('button', { name: 'parent parent-task-123456' }));
    expect(onNavigateToTask).toHaveBeenCalledWith('parent-task-123456');
    fireEvent.click(screen.getByRole('button', { name: 'Linked task linked-task-abcdef' }));
    expect(onNavigateToTask).toHaveBeenCalledWith('linked-task-abcdef');
  });

  it('guards sideboard external links while preserving legacy id URLs', () => {
    render(
      <DetailsTab
        task={makeTask({
          sideboard_context: {
            project: { id: 'proj-1', name: 'Blueprint', repo: null },
            purpose: null,
            strategic: { direction: null, cycle: null },
            pipeline: { parent_task_id: null, child_count: 0, dependency_count: 0, blocked_reason: null },
            artifacts: { total: 0, by_type: {}, primary_pr_url: 'javascript:alert(1)', primary_commit: null },
            messages: { total_recent: 0, items: [] },
            links: [
              {
                id: 'legacy_pr',
                label: 'Legacy PR',
                target: {
                  surface: 'external',
                  id: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/43',
                },
              },
              {
                id: 'unsafe_pr',
                label: 'Unsafe PR',
                target: {
                  surface: 'external',
                  id: 'legacy-unsafe-url',
                  url: 'javascript:alert(1)',
                },
              },
            ],
          },
        })}
      />,
    );

    expect(screen.queryByRole('link', { name: 'pull request' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Legacy PR github.com/HenryHamster/BlueprintAIStudio/pull/43' })).toHaveAttribute(
      'href',
      'https://github.com/HenryHamster/BlueprintAIStudio/pull/43',
    );
    expect(screen.getByText('Unsafe PR').closest('a')).toBeNull();
  });

  it('falls back to legacy direction lookup when sideboard context is absent', async () => {
    vi.mocked(authGet).mockImplementation(async (url: string) => {
      if (url.includes('/directions')) {
        return new Response(JSON.stringify({
          directions: [{ id: 'd-legacy', text: 'Legacy direction text' }],
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ stats: {} }), { status: 200 });
    });

    render(
      <DetailsTab
        task={makeTask({
          project_id: 'proj-legacy',
          context: { direction_id: 'd-legacy' },
        })}
      />,
    );

    expect(await screen.findByText('Legacy direction text')).toBeInTheDocument();
    expect(screen.getByText('d-legacy')).toBeInTheDocument();
  });
});

describe('DetailsTab — parked wake registration', () => {
  afterEach(() => cleanup());

  it('renders pending and fired wake registration details', () => {
    const pendingTask = makeTask({
      status: 'PARKED',
      wake_registration: {
        wake_source_kind: 'compute_job_terminal',
        wake_source_ref: null,
        delivery_state: 'PENDING',
        deadline_at: null,
        fired_at: null,
        result_state: null,
        result_detail: null,
      },
    });
    const firedTask = makeTask({
      status: 'PARKED',
      park_reason: 'Waiting on compute job',
      parked_at: '2026-06-01T12:00:00Z',
      wake_registration: {
        wake_source_kind: 'compute_job_terminal',
        wake_source_ref: 'job-123',
        delivery_state: 'DELIVERED',
        deadline_at: '2026-06-01T13:00:00Z',
        fired_at: '2026-06-01T12:10:00Z',
        result_state: 'resumed',
        result_detail: 'Task re-queued after compute job completed',
      },
    });

    const { rerender } = render(<DetailsTab task={pendingTask} />);

    expect(screen.getByText('compute_job_terminal')).toBeInTheDocument();
    expect(screen.getByText('no source ref')).toBeInTheDocument();
    expect(screen.getByText('Pending')).toBeInTheDocument();
    expect(screen.getByText('Not yet fired')).toBeInTheDocument();
    expect(screen.getByText('No deadline')).toBeInTheDocument();
    expect(screen.queryByText('Result')).toBeNull();

    rerender(<DetailsTab task={firedTask} />);

    expect(screen.getByText('Parked')).toBeInTheDocument();
    expect(screen.getByText('Waiting on compute job')).toBeInTheDocument();
    expect(screen.getByText('Wake source')).toBeInTheDocument();
    expect(screen.getByText('compute_job_terminal')).toBeInTheDocument();
    expect(screen.getByText('job-123')).toBeInTheDocument();
    expect(screen.getByText('Delivery')).toBeInTheDocument();
    expect(screen.getByText('Delivered')).toBeInTheDocument();
    expect(screen.getByText(formatAbsolute('2026-06-01T12:10:00Z'))).toBeInTheDocument();
    expect(screen.getByText(formatAbsolute('2026-06-01T13:00:00Z'))).toBeInTheDocument();
    expect(screen.getByText('resumed: Task re-queued after compute job completed')).toBeInTheDocument();
  });
});

describe('DetailsTab — dependency status colors', () => {
  afterEach(() => cleanup());

  function makeDepTask(status: Task['status']): Task {
    return {
      id: 'dep-1',
      title: 'Dependency task',
      description: 'dep',
      status,
      task_type: 'create',
      created_at: '2024-01-01T00:00:00Z',
      created_by: 'ORCHESTRATOR',
      requires_approval: false,
    };
  }

  it('colors a BLOCKED dependency item with amber text', () => {
    const task = makeTask({ depends_on: ['dep-1'] });
    const depTask = makeDepTask('BLOCKED');
    render(<DetailsTab task={task} tasks={[task, depTask]} />);
    const statusLabel = screen.getByText('BLOCKED');
    expect(statusLabel.className).toContain('amber');
  });

  it('colors a PARKED dependency item with purple text', () => {
    const task = makeTask({ depends_on: ['dep-1'] });
    const depTask = makeDepTask('PARKED');
    render(<DetailsTab task={task} tasks={[task, depTask]} />);
    const statusLabel = screen.getByText('PARKED');
    expect(statusLabel.className).toContain('purple');
  });
});

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
    // 0 skipped out of 100 = 0%
    expect(screen.getByText('100 passed · 0 skipped (0%) · 100 total')).toBeInTheDocument();
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
    // Percentage shown: 1500/1505 ≈ 99.67% rounds to 100%
    expect(screen.getByText('5 passed · 1500 skipped (100%) · 1505 total')).toBeInTheDocument();
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

  it('blocks and frames disabled tests as the cause when the PR self-suppressed the suite', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        noTestsReason: 'all_skipped',
        tests: [],
        output: '0 passed, 12 skipped',
        disabledTests: {
          count: 12,
          markers: [{ file: 'test_bar.py', marker: '@pytest.mark.skip', line: '1' }],
        },
      })],
    });
    render(<DetailsTab task={task} />);
    // The gate BLOCKED — not a clean pass / fall-open.
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.queryByText('Passed')).toBeNull();
    // The disabled-tests section frames the skips as the CAUSE of the block, not
    // the visibility-only quarantine wording.
    expect(screen.getByText(/this is the cause of the merge block/)).toBeInTheDocument();
    expect(screen.queryByText(/visibility only; does not block the merge/)).toBeNull();
    // And the blockedNote explains the self-suppression.
    expect(screen.getByText(/rather than accept a suite made green by skipping/)).toBeInTheDocument();
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
      task_type: 'review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'approve',
        evidence: [{ label: 'Decision', value: 'APPROVE' }],
        highlights: [{ label: 'Correctness', value: 'pass' }],
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('Approve');
    expect(badge.className).toContain('status-success');
    expect(badge.className).not.toContain('cyan');
  });

  it('colors a request_changes panel verdict as a blocking (red) badge', () => {
    const task = makeTask({
      task_type: 'review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'request_changes',
        evidence: [{ label: 'Decision', value: 'REQUEST_CHANGES' }],
        highlights: [{ label: 'Correctness', value: 'fail' }],
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('Request Changes');
    expect(badge.className).toContain('status-blocking');
    expect(badge.className).not.toContain('cyan');
  });

  it('renders the terminal compute result reference when present', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
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
        accelerator_type: null,
        accelerator_count: null,
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
        task_title: 'compute_experiment: run a budgeted job',
        task_type: 'compute_experiment',
      },
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    expect(fetchComputeJobs).toHaveBeenCalledWith('proj-1', undefined, undefined, 0, 'task-verify-1');
    expect(await screen.findByRole('link', { name: /https:\/\/storage\.example\.com\/jobs\/job-1\/result\.json/i })).toHaveAttribute(
      'href',
      'https://storage.example.com/jobs/job-1/result.json',
    );
  });

  it('surfaces COMPUTE_RESULT artifact as the primary outcome with a resolvable reference', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({
        artifact_id: 'artifact-xyz',
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
      }),
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    const artifact = await screen.findByText('COMPUTE_RESULT artifact');
    expect(artifact.closest('a')).toHaveAttribute(
      'href',
      'https://storage.example.com/jobs/job-1/result.json',
    );
    // Raw storage URI demoted to a secondary technical detail row.
    expect(screen.getByText('Result reference')).toBeInTheDocument();
  });

  it('surfaces COMPUTE_RESULT artifact even when result_reference is null (non-openable)', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ artifact_id: 'artifact-xyz', result_reference: null }),
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    const artifact = await screen.findByText('COMPUTE_RESULT artifact');
    expect(artifact.closest('a')).toBeNull();
    expect(artifact.closest('button')).toBeNull();
    expect(screen.queryByText('Result reference')).toBeNull();
  });

  it('shows no artifact affordance when artifact_id is absent', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ artifact_id: null, result_reference: null }),
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    await screen.findByText('gcp_compute_engine');
    expect(screen.queryByText('COMPUTE_RESULT artifact')).toBeNull();
  });

  it('resolves a gs:// compute result through the admin API before opening', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue({ opener: null, location: { replace: vi.fn() }, close: vi.fn() } as never);
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
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
        accelerator_type: null,
        accelerator_count: null,
        artifact_id: null,
        artifact: null,
        result_reference: 'gs://blueprint-agent-artifacts/tasks/task-verify-1/result.json',
        task_title: 'compute_experiment: run a budgeted job',
        task_type: 'compute_experiment',
      },
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    const button = await screen.findByRole('button', { name: /gs:\/\/blueprint-agent-artifacts\/tasks\/task-verify-1\/result\.json/i });
    button.click();
    expect(await screen.findByText('gs://blueprint-agent-artifacts/tasks/task-verify-1/result.json')).toBeInTheDocument();
    expect(resolveArtifactDownloadUrl).toHaveBeenCalledWith(
      'proj-1',
      'gs://blueprint-agent-artifacts/tasks/task-verify-1/result.json',
    );
    expect(openSpy).toHaveBeenCalled();
  });

  it('renders linked compute-result artifacts distinctly from raw result references', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
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
        accelerator_type: null,
        accelerator_count: null,
        artifact_id: 'artifact-1',
        artifact: {
          id: 'artifact-1',
          artifact_type: 'COMPUTE_RESULT',
          storage_uri: 'gs://blueprint-agent-artifacts/tasks/task-verify-1/artifacts/result.json',
          logical_key: 'tasks/task-verify-1/artifacts/result.json',
          created_at: '2026-01-01T00:10:00Z',
        },
        result_reference: 'https://storage.example.com/jobs/job-1/result.json',
        task_title: 'compute_experiment: run a budgeted job',
        task_type: 'compute_experiment',
      },
      {
        id: 'job-2',
        project_id: 'proj-1',
        task_id: 'task-verify-1',
        backend: 'gcp_compute_engine',
        external_compute_id: 'compute-job-vm-2',
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
        accelerator_type: null,
        accelerator_count: null,
        artifact_id: null,
        artifact: null,
        result_reference: 'https://storage.example.com/jobs/job-2/result.json',
        task_title: 'compute_experiment: run a budgeted job',
        task_type: 'compute_experiment',
      },
    ]));
    const task = makeTask({ project_id: 'proj-1' as unknown as Task['project_id'] });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('Compute Result Artifact')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /gs:\/\/blueprint-agent-artifacts\/tasks\/task-verify-1\/artifacts\/result\.json/i })).toBeInTheDocument();
    expect(screen.getByText('Raw result reference')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /https:\/\/storage\.example\.com\/jobs\/job-2\/result\.json/i })).toBeInTheDocument();
  });

  it('renders storage-backed task artifacts through the signed download resolver', () => {
    const task = makeTask({
      project_id: 'proj-1' as unknown as Task['project_id'],
      artifacts: [
        {
          artifact_type: 'COMPUTE_RESULT',
          storage_uri: 'gs://blueprint-agent-artifacts/tasks/task-verify-1/artifacts/metrics.json',
          iteration: 2,
          created_at: '2026-01-01T00:10:00Z',
        },
      ],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Artifacts')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /gs:\/\/blueprint-agent-artifacts\/tasks\/task-verify-1\/artifacts\/metrics\.json/i })).toBeInTheDocument();
  });


  it('shows the fall-open reason when a suite was detected but every test was skipped', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        noTestsReason: 'all_skipped',
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('No suite detected')).toBeInTheDocument();
    expect(screen.getByText('Fall-open reason')).toBeInTheDocument();
    expect(screen.getByText(/every test was skipped/)).toBeInTheDocument();
  });

  it('shows the fall-open reason when no real tests were found (placeholder/empty)', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        noTestsReason: 'no_real_tests',
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('No suite detected')).toBeInTheDocument();
    expect(screen.getByText('Fall-open reason')).toBeInTheDocument();
    expect(screen.getByText(/no real tests were found/)).toBeInTheDocument();
  });

  it('shows the fall-open reason when genuinely no suite was detectable', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: false,
        passed: false,
        tests: [],
        output: '',
        noTestsReason: 'no_suite',
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('No suite detected')).toBeInTheDocument();
    expect(screen.getByText('Fall-open reason')).toBeInTheDocument();
    expect(screen.getByText(/no suite was detected/)).toBeInTheDocument();
  });

  it('does not show fall-open reason for legacy artifacts without noTestsReason', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({ detected: false, passed: false, tests: [], output: '' })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('No suite detected')).toBeInTheDocument();
    expect(screen.queryByText('Fall-open reason')).toBeNull();
  });

  it('includes skip percentage in coverage display', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        coverage: { passed: 80, skipped: 20, total: 100 },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Coverage')).toBeInTheDocument();
    // Percentage should be shown: 20 skipped out of 100 = 20%
    expect(screen.getByText('80 passed · 20 skipped (20%) · 100 total')).toBeInTheDocument();
  });

  it('handles edge case of zero total tests in coverage', () => {
    const task = makeTask({
      artifacts: [mergeTestArtifact({
        detected: true,
        passed: true,
        tests: [],
        output: '',
        coverage: { passed: 0, skipped: 0, total: 0 },
      })],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Coverage')).toBeInTheDocument();
    // Should show 0% when total is 0
    expect(screen.getByText('0 passed · 0 skipped (0%) · 0 total')).toBeInTheDocument();
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
    let resolveDocument!: (document: { id: string; body: string }) => void;
    const fullDocument = new Promise<{ id: string; body: string }>((resolve) => {
      resolveDocument = resolve;
    });
    vi.mocked(fetchKnowledgeDocument).mockReturnValue(fullDocument as never);
    render(<DetailsTab task={knowledgeTask()} />);
    await act(async () => {
      fireEvent.click(screen.getByText('View document'));
      resolveDocument({ id: 'doc-1', body: '# Full body\n\nDetailed markdown.' });
      await fullDocument;
    });
    expect(fetchKnowledgeDocument).toHaveBeenCalledWith('proj-1', 'doc-1');
    expect(screen.getByText(/Detailed markdown/)).toBeInTheDocument();
  });

  it('surfaces live-proof release evidence (promoted SHA + serving status) after the full-doc fetch', async () => {
    const { fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({
      id: 'doc-1',
      body: '# Full body',
      release_evidence: {
        promoted_sha: 'abcdef1234567890deadbeef',
        aggregate_serving_status: 'serving_confirmed',
        release_version_number: 8,
      },
    } as never);
    render(<DetailsTab task={knowledgeTask()} />);
    screen.getByText('View document').click();
    const chip = await screen.findByTestId('release-evidence-chip');
    expect(chip).toBeInTheDocument();
    expect(chip.textContent).toContain('v8');
    expect(chip.textContent).toContain('abcdef12');
    expect(chip.textContent).not.toContain('abcdef1234567890deadbeef');
  });

  it('shows no release-evidence chip when the document has none', async () => {
    const { fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({
      id: 'doc-1',
      body: '# Full body',
      release_evidence: null,
    } as never);
    render(<DetailsTab task={knowledgeTask()} />);
    screen.getByText('View document').click();
    expect(await screen.findByText(/Full body/)).toBeInTheDocument();
    expect(screen.queryByTestId('release-evidence-chip')).toBeNull();
  });

  it('knowledge_document_section_surfaces_body_load_failure', async () => {
    const { fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockRejectedValueOnce(new Error('Network error'));
    render(<DetailsTab task={knowledgeTask()} />);
    screen.getByText('View document').click();
    expect(await screen.findByText('Failed to load document body.')).toBeInTheDocument();
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

  it('displays base re-cut hold section when task has merge conflict', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: {
        _recut_recheck_attempts: 5,
        _recut_last_outcome: 'conflict',
        _recut_conflict_attempts: 3,
        _recut_escalated: true,
      },
      payloads: [{
        type: 'create',
      }],
    });
    render(<DetailsTab task={task} />);
    const heading = screen.getByText('Base Re-cut Hold');
    expect(heading).toBeInTheDocument();
    const section = heading.closest('div')?.parentElement;
    expect(section?.textContent).toContain('Held pending re-cut of base');
    expect(section?.textContent).toContain('from integration');
    expect(section?.textContent).toContain('Conflict');
    expect(section?.textContent).toContain('5');
    expect(section?.textContent).toContain('Escalated');
  });

  it('displays base re-cut hold section when task has transient error', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: {
        _recut_recheck_attempts: 2,
        _recut_last_outcome: 'error',
        _recut_conflict_attempts: 0,
      },
      payloads: [{
        type: 'fix',
      }],
    });
    render(<DetailsTab task={task} />);
    const heading = screen.getByText('Base Re-cut Hold');
    expect(heading).toBeInTheDocument();
    const section = heading.closest('div')?.parentElement;
    expect(section?.textContent).toContain('Held pending re-cut of base');
    expect(section?.textContent).toContain('from integration');
    expect(section?.textContent).toContain('Transient Error');
    expect(section?.textContent).toContain('2');
    expect(section?.textContent).not.toContain('Escalated');
  });

  it('hides base re-cut hold section when no re-cut context exists', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: {},
    });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Base Re-cut Hold')).toBeNull();
  });

  it('shows (default) base placeholder and integration as re-cut source when base_branch not specified', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: {
        _recut_recheck_attempts: 1,
        _recut_last_outcome: 'conflict',
      },
      payloads: [{ type: 'create' }],
    });
    render(<DetailsTab task={task} />);
    const heading = screen.getByText('Base Re-cut Hold');
    expect(heading).toBeInTheDocument();
    const section = heading.closest('div')?.parentElement;
    expect(section?.textContent).toContain('Held pending re-cut of base');
    expect(section?.textContent).toContain('(default)');
    expect(section?.textContent).toContain('from integration');
  });
});

describe('DetailsTab — Research Origin Spawn Gate', () => {
  afterEach(() => cleanup());

  function makeResearchChildTask(spawnGateCtx: Record<string, unknown> = {}): Task {
    return makeTask({
      context: {
        research_task_id: 'research-task-1',
        finding_index: 0,
        ...spawnGateCtx,
      },
    });
  }

  it('renders no Spawn Gate row for a non-research-derived task', () => {
    render(<DetailsTab task={makeTask()} />);
    expect(screen.queryByText('Spawn Gate')).toBeNull();
  });

  it('renders unchallenged badge when gate did not run on the finding', () => {
    const task = makeResearchChildTask({ research_spawn_gate_challenged: false });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Spawn Gate')).toBeInTheDocument();
    expect(screen.getByText('unchallenged')).toBeInTheDocument();
  });

  it('renders challenged-pass badge with bounded reason', () => {
    const task = makeResearchChildTask({
      research_spawn_gate_challenged: true,
      research_spawn_gate_verdict: 'pass',
      research_spawn_gate_explicit: true,
      research_spawn_gate_reason: 'Challenger confirmed actionability',
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('challenged — pass')).toBeInTheDocument();
    expect(screen.getByText('Challenger confirmed actionability')).toBeInTheDocument();
    // Challenged-pass badge should be emerald (green)
    expect(screen.getByText('challenged — pass').className).toContain('emerald');
  });

  it('renders fail-open-pass badge with amber styling when explicit is false', () => {
    const task = makeResearchChildTask({
      research_spawn_gate_challenged: true,
      research_spawn_gate_verdict: 'pass',
      research_spawn_gate_explicit: false,
      research_spawn_gate_reason: 'fail-open: no verdict returned for this finding',
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('fail-open pass')).toBeInTheDocument();
    expect(screen.getByText(/fail-open: no verdict returned/)).toBeInTheDocument();
    // Fail-open badge should be amber (risky)
    expect(screen.getByText('fail-open pass').className).toContain('amber');
  });

  it('renders Spawn Gate row without reason when reason is null', () => {
    const task = makeResearchChildTask({
      research_spawn_gate_challenged: true,
      research_spawn_gate_verdict: 'pass',
      research_spawn_gate_explicit: true,
      research_spawn_gate_reason: null,
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('challenged — pass')).toBeInTheDocument();
    // No reason text should appear
    expect(screen.queryByText(/Challenger confirmed/)).toBeNull();
  });

  it('renders unchallenged badge when research_spawn_gate_challenged is absent', () => {
    // No spawn gate keys at all — task was spawned from research but gate didn't run
    const task = makeResearchChildTask({});
    render(<DetailsTab task={task} />);
    // research_spawn_gate_challenged is absent/falsy → unchallenged
    expect(screen.getByText('unchallenged')).toBeInTheDocument();
  });
});

describe('DetailsTab — Liveness signals', () => {
  afterEach(() => cleanup());

  it('shows the last tool use as a relative time and falls back when absent', () => {
    const now = new Date('2026-06-08T12:00:00Z').getTime();
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now);
    const task = makeTask({
      last_tool_use_at: new Date(now - 5 * 60 * 1000).toISOString(),
    });

    render(<DetailsTab task={task} />);

    expect(screen.getByText('Last tool use')).toBeInTheDocument();
    expect(screen.getByText('5m ago')).toBeInTheDocument();

    nowSpy.mockRestore();

    cleanup();

    render(<DetailsTab task={makeTask()} />);
    expect(screen.getByText('No tool use recorded')).toBeInTheDocument();
  });
});

describe('DetailsTab — p50 ETA progress for active tasks', () => {
  afterEach(() => {
    vi.mocked(authGet).mockResolvedValue(new Response(JSON.stringify({ stats: {} }), { status: 200 }));
    cleanup();
  });

  it('shows p50 duration and progress percentage when task is IN_PROGRESS', async () => {
    const now = new Date('2026-06-08T12:00:00Z').getTime();
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now);
    const startedAt = new Date(now - 30 * 60 * 1000).toISOString(); // 30 minutes ago

    vi.mocked(authGet).mockResolvedValue(
      new Response(JSON.stringify({ stats: { create: { p50_seconds: 3600, sample_count: 50 } } }), { status: 200 }),
    );

    const task = makeTask({
      status: 'IN_PROGRESS',
      task_type: 'create',
      started_at: startedAt,
      project_id: 'p50-test-progress',
    });

    render(<DetailsTab task={task} />);

    // Check for p50 duration row showing 1h
    expect(await screen.findByText('p50 Duration')).toBeInTheDocument();
    expect(screen.getByText('1h')).toBeInTheDocument();

    // Check for progress row showing ~50% progress
    expect(screen.getByText('Progress')).toBeInTheDocument();
    expect(screen.getByText(/50% Within p50/)).toBeInTheDocument();

    // Check for est. remaining showing ~30m left
    expect(screen.getByText('Est. remaining')).toBeInTheDocument();
    expect(screen.getByText(/~30m left/)).toBeInTheDocument();

    nowSpy.mockRestore();
  });

  it('marks task as Over p50 when elapsed time exceeds p50 duration', async () => {
    const now = new Date('2026-06-08T12:00:00Z').getTime();
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now);
    const startedAt = new Date(now - 90 * 60 * 1000).toISOString(); // 90 minutes ago

    vi.mocked(authGet).mockResolvedValue(
      new Response(JSON.stringify({ stats: { create: { p50_seconds: 3600, sample_count: 50 } } }), { status: 200 }),
    );

    const task = makeTask({
      status: 'IN_PROGRESS',
      task_type: 'create',
      started_at: startedAt,
      project_id: 'p50-test-over',
    });

    render(<DetailsTab task={task} />);

    // Should show Over p50 and amber color
    const progressText = await screen.findByText(/Over p50/);
    expect(progressText).toBeInTheDocument();
    expect(progressText.className).toContain('amber');

    // Est. remaining should show "longer than usual" in amber
    expect(screen.getByText('longer than usual')).toBeInTheDocument();

    nowSpy.mockRestore();
  });

  it('hides p50 progress when duration stats are unavailable', async () => {
    const now = new Date('2026-06-08T12:00:00Z').getTime();
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now);
    const startedAt = new Date(now - 10 * 60 * 1000).toISOString();

    // base mock returns { stats: {} } — no override needed
    const task = makeTask({
      status: 'IN_PROGRESS',
      task_type: 'create',
      started_at: startedAt,
      project_id: 'p50-test-unavail',
    });

    render(<DetailsTab task={task} />);

    // Wait for the async fetch to settle, then confirm no p50 rows rendered
    await waitFor(() => {
      expect(screen.queryByText('p50 Duration')).toBeNull();
      expect(screen.queryByText('Progress')).toBeNull();
    });

    nowSpy.mockRestore();
  });

  it('hides p50 progress for completed tasks', async () => {
    const now = new Date('2026-06-08T12:00:00Z').getTime();
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now);
    const startedAt = new Date(now - 30 * 60 * 1000).toISOString();

    vi.mocked(authGet).mockResolvedValue(
      new Response(JSON.stringify({ stats: { create: { p50_seconds: 3600, sample_count: 50 } } }), { status: 200 }),
    );

    const task = makeTask({
      status: 'COMPLETED',
      task_type: 'create',
      started_at: startedAt,
      project_id: 'p50-test-completed',
    });

    render(<DetailsTab task={task} />);

    // Wait for fetch to settle; completed tasks should never show p50 rows
    await waitFor(() => {
      expect(screen.queryByText('p50 Duration')).toBeNull();
      expect(screen.queryByText('Progress')).toBeNull();
    });

    nowSpy.mockRestore();
  });
});

// ── Compute experiment contract helpers ──────────────────────────────────────

function makeComputeJob(overrides: Partial<import('../api').ComputeJob> = {}): import('../api').ComputeJob {
  return {
    id: 'job-exp-1',
    project_id: 'proj-1',
    task_id: 'task-verify-1',
    backend: 'gcp_compute_engine',
    external_compute_id: 'vm-instance-1',
    machine_type: 'n1-standard-4',
    status: 'SUCCEEDED',
    terminal_reason: null,
    outcome: 'SUCCESS',
    spend_cap_usd: 10,
    wall_clock_deadline: '2026-06-08T01:00:00Z',
    created_at: '2026-06-08T00:00:00Z',
    updated_at: '2026-06-08T00:30:00Z',
    started_at: '2026-06-08T00:00:00Z',
    completed_at: '2026-06-08T00:30:00Z',
    last_polled_at: '2026-06-08T00:30:00Z',
    accrued_cost_usd: 3.5,
    final_cost_usd: 3.5,
    artifact_id: null,
    artifact: null,
    result_reference: null,
    accelerator_type: null,
    accelerator_count: null,
    task_title: 'compute_experiment: benchmark run',
    task_type: 'compute_experiment',
    ...overrides,
  };
}

describe('DetailsTab — Compute Experiment Contract', () => {
  afterEach(() => cleanup());

  it('renders the launch contract block for compute_experiment tasks', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([makeComputeJob()]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{
        type: 'compute_experiment',
        command: 'python3 benchmark.py --epochs 10',
        spend_cap_usd: 10,
        deadline_seconds: 3600,
      }],
    });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('Launch Contract')).toBeInTheDocument();
    expect(screen.getByText('python3 benchmark.py --epochs 10')).toBeInTheDocument();
    expect(screen.getByText('$10.00')).toBeInTheDocument();
    expect(screen.getByText('1h 0m')).toBeInTheDocument();
  });

  it('renders success criterion row when present on the payload', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([makeComputeJob()]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{
        type: 'compute_experiment',
        command: 'python3 eval.py',
        spend_cap_usd: 5,
        deadline_seconds: 1800,
        success_criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
      }],
    });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('Success criterion')).toBeInTheDocument();
    expect(screen.getByText('accuracy >= 0.9')).toBeInTheDocument();
  });

  it('renders machine type from the ComputeJob for compute_experiment tasks', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([makeComputeJob({ machine_type: 'n1-highmem-8' })]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 5, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('n1-highmem-8')).toBeInTheDocument();
  });

  it('renders accelerator type and count from the ComputeJob', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ accelerator_type: 'T4', accelerator_count: 2 }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 5, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('Accelerator')).toBeInTheDocument();
    expect(screen.getByText(/2× T4/)).toBeInTheDocument();
  });

  it('styles a SPEND_CAP_EXCEEDED kill as amber, not red', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ status: 'FAILED', outcome: 'SPEND_CAP_EXCEEDED', terminal_reason: 'budget exhausted' }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 10, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    const badge = await screen.findByText('Killed: budget cap');
    expect(badge.className).toContain('amber');
    expect(badge.className).not.toContain('red');
  });

  it('styles a TIMED_OUT kill as amber, not red', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ status: 'TIMED_OUT', outcome: 'TIMED_OUT', terminal_reason: 'deadline exceeded' }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 10, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    const badge = await screen.findByText('Killed: deadline');
    expect(badge.className).toContain('amber');
    expect(badge.className).not.toContain('red');
  });

  it('styles an operator-cancelled job as amber, not red', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ status: 'CANCELLED', outcome: 'CANCELLED', terminal_reason: 'cancelled by operator' }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 10, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    const badge = await screen.findByText('Killed by operator');
    expect(badge.className).toContain('amber');
    expect(badge.className).not.toContain('red');
  });

  it('styles a genuine FAILURE as red, not amber', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ status: 'FAILED', outcome: 'FAILURE', terminal_reason: 'process exited with code 1' }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 10, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    const badge = await screen.findByText('Failed');
    expect(badge.className).toContain('red');
    expect(badge.className).not.toContain('amber');
  });

  it('shows terminal_reason text when non-null on a terminal job', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([
      makeComputeJob({ status: 'FAILED', outcome: 'FAILURE', terminal_reason: 'OOM killed at epoch 5' }),
    ]));
    const task = makeTask({
      task_type: 'compute_experiment',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'compute_experiment', command: 'run.sh', spend_cap_usd: 10, deadline_seconds: 600 }],
    });
    render(<DetailsTab task={task} />);
    expect(await screen.findByText('OOM killed at epoch 5')).toBeInTheDocument();
  });

  it('omits the launch contract block for non-compute_experiment task types', async () => {
    vi.mocked(fetchComputeJobs).mockResolvedValueOnce(wrapJobs([makeComputeJob({ task_type: 'create' })]));
    const task = makeTask({
      task_type: 'create',
      project_id: 'proj-1' as unknown as Task['project_id'],
      payloads: [{ type: 'create', prompt: 'do something' }],
    });
    render(<DetailsTab task={task} />);
    await screen.findByText('Compute Jobs');
    expect(screen.queryByText('Launch Contract')).toBeNull();
    expect(screen.queryByText('Success criterion')).toBeNull();
  });

  it('shows analysis_mode badge on the knowledge document section', () => {
    const task = makeTask({
      task_type: 'compute_experiment',
      plan_output: {
        knowledge_document: {
          id: 'doc-compute-1',
          title: 'Benchmark Results: GPU throughput',
          summary: 'The model achieves 95% accuracy.',
          citation_count: 2,
          analysis_mode: 'llm',
          source_kind: 'compute_experiment',
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Knowledge Document')).toBeInTheDocument();
    expect(screen.getByText('Benchmark Results: GPU throughput')).toBeInTheDocument();
    expect(screen.getByText('llm')).toBeInTheDocument();
    expect(screen.getByText('compute experiment')).toBeInTheDocument();
  });

  it('shows compute acceptance grade on the knowledge document section — acceptance grade', () => {
    const task = makeTask({
      task_type: 'compute_experiment',
      plan_output: {
        knowledge_document: {
          id: 'doc-compute-2',
          title: 'Benchmark Results: GPU throughput',
          summary: 'The model achieves 95% accuracy.',
          citation_count: 3,
          analysis_mode: 'llm',
          source_kind: 'compute_experiment',
          compute_acceptance_grade: 'acceptance',
          compute_acceptance_reason: 'llm_artifact_cited',
        },
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('Acceptance');
    expect(badge).toBeInTheDocument();
    expect(badge.closest('[title]')?.getAttribute('title')).toContain('LLM-grounded');
  });

  it('shows compute acceptance grade on the knowledge document section — measured-only grade', () => {
    const task = makeTask({
      task_type: 'compute_experiment',
      plan_output: {
        knowledge_document: {
          id: 'doc-compute-3',
          title: 'Deterministic Benchmark Summary',
          summary: 'Fallback analysis only.',
          citation_count: 0,
          analysis_mode: 'fallback',
          source_kind: 'compute_experiment',
          compute_acceptance_grade: 'measured_only',
          compute_acceptance_reason: 'analysis_mode_not_llm',
          analysis_fallback_reason: 'precondition-not-met',
        },
      },
    });
    render(<DetailsTab task={task} />);
    const badge = screen.getByText('Measured only');
    expect(badge).toBeInTheDocument();
    expect(badge.closest('[title]')?.getAttribute('title')).toContain('Missing credentials');
    expect(screen.getByText('Missing credentials — analysis skipped')).toBeInTheDocument();
  });

  it('prefers unavailable_cause labels over fallback reasons', () => {
    const task = makeTask({
      task_type: 'compute_experiment',
      plan_output: {
        knowledge_document: {
          id: 'doc-compute-4',
          title: 'Unavailable Analysis',
          summary: 'Analysis could not run.',
          citation_count: 0,
          analysis_mode: 'unavailable',
          unavailable_cause: 'analysis-disabled',
          analysis_fallback_reason: 'precondition-not-met',
          source_kind: 'compute_experiment',
          compute_acceptance_grade: 'measured_only',
          compute_acceptance_reason: 'analysis_mode_not_llm',
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Measured only')).toBeInTheDocument();
    expect(screen.getByText('Analysis disabled')).toBeInTheDocument();
    expect(screen.queryByText('Missing credentials — analysis skipped')).toBeNull();
  });

  it('renders legacy measured-only compute docs without cause fields', () => {
    const task = makeTask({
      task_type: 'compute_experiment',
      plan_output: {
        knowledge_document: {
          id: 'doc-compute-5',
          title: 'Legacy Measured Summary',
          summary: 'Older backend response without cause metadata.',
          citation_count: 1,
          source_kind: 'compute_experiment',
          compute_acceptance_grade: 'measured_only',
          compute_acceptance_reason: 'analysis_mode_not_llm',
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Measured only')).toBeInTheDocument();
    expect(screen.queryByText('Analysis disabled')).toBeNull();
    expect(screen.queryByText('Missing credentials — analysis skipped')).toBeNull();
  });

  it('does not show acceptance grade badge for non-compute documents', () => {
    const task = makeTask({
      task_type: 'design',
      plan_output: {
        knowledge_document: {
          id: 'doc-design-1',
          title: 'Design Document',
          source_kind: 'design',
          compute_acceptance_grade: null,
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Knowledge Document')).toBeInTheDocument();
    expect(screen.queryByText('Acceptance')).toBeNull();
    expect(screen.queryByText('Measured only')).toBeNull();
  });
});

describe('DetailsTab — adversarial gate diagnostic', () => {
  afterEach(() => cleanup());

  it('renders parse state and runtime provenance fields', () => {
    const vmImageBuildTs = 1749168000;
    const vmRuntimeFingerprint = 'runtime-fingerprint-abcdef1234567890';
    const task = makeTask({
      context: {
        adversarial_gate_diagnostic: {
          challenger_task_id: 'grader-task-1',
          completion_state: 'completed',
          challenged_count: 2,
          parse_state: 'no_hash_matches',
          parse_cause: 'JSON array contained no known finding hashes',
          parsed_verdict_count: 0,
          unresolved_count: 2,
          vm_image_name: 'blueprint/grader:2026-06-05',
          vm_image_build_ts: vmImageBuildTs,
          vm_runtime_fingerprint: vmRuntimeFingerprint,
          vm_source_sha: '0123456789abcdef0123456789abcdef01234567',
        },
      },
    });

    render(<DetailsTab task={task} />);

    expect(screen.getByText('Adversarial Gate Diagnostic')).toBeInTheDocument();
    expect(screen.getByText('no hash matches')).toBeInTheDocument();
    expect(screen.getByText('JSON array contained no known finding hashes')).toBeInTheDocument();
    expect(screen.getByTitle(new Date(vmImageBuildTs * 1000).toISOString())).toBeInTheDocument();
    expect(screen.getByTitle(vmRuntimeFingerprint)).toBeInTheDocument();
    expect(screen.getByText('runtime-fing')).toBeInTheDocument();
  });
});

describe('DetailsTab — Acceptance Evidence', () => {
  afterEach(() => cleanup());

  it('renders nothing when code_acceptance_verdict is absent', () => {
    const task = makeTask({ context: {} });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Acceptance Evidence')).toBeNull();
  });

  it('renders a proven badge for a machine-checked satisfied verdict', () => {
    const task = makeTask({
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'named_test',
          reason: "named_test 'testing/test_foo.py' was added/modified and ran green",
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Acceptance Evidence')).toBeInTheDocument();
    expect(screen.getByText('proven')).toBeInTheDocument();
    expect(screen.getByText('named_test')).toBeInTheDocument();
    expect(screen.getByText(/ran green/)).toBeInTheDocument();
    expect(screen.queryByText('View screenshot')).toBeNull();
  });

  it('renders a satisfied badge for a soft/unproven satisfied verdict', () => {
    const task = makeTask({
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: false,
          kind: 'observable_behavior',
          reason: 'declared observable_behavior shipped with a real diff',
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('satisfied')).toBeInTheDocument();
    expect(screen.getByText('observable_behavior')).toBeInTheDocument();
    // Satisfied badge should be amber
    expect(screen.getByText('satisfied').className).toContain('amber');
  });

  it('renders a fail-closed badge for an unsatisfied verdict', () => {
    const task = makeTask({
      context: {
        code_acceptance_verdict: {
          satisfied: false,
          proven: false,
          kind: 'named_test',
          reason: "declared named_test 'testing/test_missing.py' is not among the PR's changed files",
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('fail-closed')).toBeInTheDocument();
    expect(screen.getByText(/not among the PR/)).toBeInTheDocument();
    expect(screen.getByText('fail-closed').className).toContain('red');
  });

  it('shows View screenshot toggle for kind=visual', () => {
    const task = makeTask({
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'visual',
          reason: 'visual acceptance evidence matches the uploaded viewport screenshot',
        },
      },
      payloads: [{ type: 'create', acceptance_evidence: { kind: 'visual', component: 'DashboardHome', viewport_width: 1280 } }],
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('View screenshot')).toBeInTheDocument();
  });

  it('fetches and displays screenshot image when toggle is clicked', async () => {
    vi.mocked(resolveViewportFailureDownloadUrl).mockResolvedValueOnce('https://signed.example.com/viewport/screenshot.png');
    const task = makeTask({
      project_id: 'proj-1' as unknown as Task['project_id'],
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'visual',
          reason: 'visual acceptance evidence matches the uploaded viewport screenshot',
        },
      },
      payloads: [{ type: 'create', acceptance_evidence: { kind: 'visual', component: 'DashboardHome', viewport_width: 1280 } }],
    });
    render(<DetailsTab task={task} />);
    const toggleBtn = screen.getByText('View screenshot');
    fireEvent.click(toggleBtn);
    expect(resolveViewportFailureDownloadUrl).toHaveBeenCalledWith('proj-1', 'DashboardHome', 1280);
    const img = await screen.findByRole('img', { name: /viewport screenshot/i });
    expect(img).toHaveAttribute('src', 'https://signed.example.com/viewport/screenshot.png');
  });

  it('shows error text when screenshot API rejects', async () => {
    vi.mocked(resolveViewportFailureDownloadUrl).mockRejectedValueOnce(new Error('Screenshot not found'));
    const task = makeTask({
      project_id: 'proj-1' as unknown as Task['project_id'],
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'visual',
          reason: 'visual acceptance evidence matches the uploaded viewport screenshot',
        },
      },
      payloads: [{ type: 'create', acceptance_evidence: { kind: 'visual', component: 'DashboardHome', viewport_width: 375 } }],
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText('View screenshot'));
    await waitFor(() => expect(screen.getByText('Screenshot not found')).toBeInTheDocument());
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('does not show screenshot toggle for non-visual kinds', () => {
    const task = makeTask({
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'downstream_artifact',
          reason: "downstream_artifact 'release-deploy-artifact' is reflected in the shipped changed files",
        },
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Acceptance Evidence')).toBeInTheDocument();
    expect(screen.queryByText('View screenshot')).toBeNull();
  });

  it('hides screenshot when toggle is clicked a second time', async () => {
    vi.mocked(resolveViewportFailureDownloadUrl).mockResolvedValue('https://signed.example.com/viewport/screenshot.png');
    const task = makeTask({
      project_id: 'proj-1' as unknown as Task['project_id'],
      context: {
        code_acceptance_verdict: {
          satisfied: true,
          proven: true,
          kind: 'visual',
          reason: 'visual acceptance evidence matches',
        },
      },
      payloads: [{ type: 'create', acceptance_evidence: { kind: 'visual', component: 'ImprovementLanesPanel', viewport_width: 375 } }],
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText('View screenshot'));
    await screen.findByRole('img', { name: /viewport screenshot/i });
    fireEvent.click(screen.getByText('Hide screenshot'));
    expect(screen.queryByRole('img')).toBeNull();
  });
});

describe('DetailsTab — ViewportScreenshotsPanel', () => {
  afterEach(() => cleanup());

  it('renders nothing when viewport_screenshots is absent', () => {
    const task = makeTask({ context: {} });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Viewport Screenshots')).toBeNull();
  });

  it('renders nothing when viewport_screenshots is empty', () => {
    const task = makeTask({ context: { viewport_screenshots: [] } });
    render(<DetailsTab task={task} />);
    expect(screen.queryByText('Viewport Screenshots')).toBeNull();
  });

  it('shows collapsed toggle with screenshot count', () => {
    const task = makeTask({
      context: {
        viewport_screenshots: [
          { name: 'taskcard-default-375-before', storage_uri: 'gs://bucket/a.png' },
          { name: 'taskcard-default-375-after', storage_uri: 'gs://bucket/b.png' },
        ],
      },
    });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Viewport Screenshots')).toBeInTheDocument();
    expect(screen.getByText(/View 2 screenshots/)).toBeInTheDocument();
  });

  it('fetches and displays before/after images side-by-side on expand', async () => {
    vi.mocked(resolveViewportShotDownloadUrl)
      .mockResolvedValueOnce('https://cdn.example.com/before.png')
      .mockResolvedValueOnce('https://cdn.example.com/after.png');
    const task = makeTask({
      id: 'task-vs-1',
      project_id: 'proj-vs' as unknown as Task['project_id'],
      context: {
        viewport_screenshots: [
          { name: 'taskcard-default-375-before', storage_uri: 'gs://bucket/a.png' },
          { name: 'taskcard-default-375-after', storage_uri: 'gs://bucket/b.png' },
        ],
      },
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText(/View 2 screenshots/));
    expect(screen.getByText('Before')).toBeInTheDocument();
    expect(screen.getByText('After')).toBeInTheDocument();
    expect(resolveViewportShotDownloadUrl).toHaveBeenCalledWith('proj-vs', 'task-vs-1', 'taskcard-default-375-before', expect.anything());
    expect(resolveViewportShotDownloadUrl).toHaveBeenCalledWith('proj-vs', 'task-vs-1', 'taskcard-default-375-after', expect.anything());
    const imgs = await screen.findAllByRole('img');
    expect(imgs.some(img => img.getAttribute('src') === 'https://cdn.example.com/before.png')).toBe(true);
    expect(imgs.some(img => img.getAttribute('src') === 'https://cdn.example.com/after.png')).toBe(true);
  });

  it('collapses on second click and cleans up images', async () => {
    vi.mocked(resolveViewportShotDownloadUrl).mockResolvedValue('https://cdn.example.com/shot.png');
    const task = makeTask({
      context: {
        viewport_screenshots: [
          { name: 'component-375-before', storage_uri: 'gs://bucket/a.png' },
        ],
      },
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText(/View 1 screenshot/));
    await screen.findByRole('img');
    fireEvent.click(screen.getByText('Hide screenshots'));
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('shows error text when URL resolution fails', async () => {
    vi.mocked(resolveViewportShotDownloadUrl).mockRejectedValueOnce(new Error('Storage unavailable'));
    const task = makeTask({
      project_id: 'proj-err' as unknown as Task['project_id'],
      context: {
        viewport_screenshots: [
          { name: 'comp-375-before', storage_uri: 'gs://bucket/a.png' },
        ],
      },
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText(/View 1 screenshot/));
    await waitFor(() => expect(screen.getByText('Storage unavailable')).toBeInTheDocument());
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('renders magnitude badge and Show diff toggle when after entry carries magnitude', async () => {
    vi.mocked(resolveViewportShotDownloadUrl).mockResolvedValue('https://cdn.example.com/shot.png');
    const task = makeTask({
      id: 'task-vs-mag',
      project_id: 'proj-mag' as unknown as Task['project_id'],
      context: {
        viewport_screenshots: [
          { name: 'comp-375-before', storage_uri: 'gs://bucket/a.png' },
          { name: 'comp-375-after', magnitude: 0.42, storage_uri: 'gs://bucket/b.png' },
          { name: 'comp-375-diff', storage_uri: 'gs://bucket/d.png' },
        ],
      },
    });
    render(<DetailsTab task={task} />);
    fireEvent.click(screen.getByText(/View 3 screenshots/));
    expect(screen.getByText('42.0% changed')).toBeInTheDocument();
    expect(screen.getByText('Show diff')).toBeInTheDocument();
  });
});

describe('DetailsTab — parent/child task health', () => {
  afterEach(() => cleanup());

  it('renders parent and child task color-coded status + title for related tasks present in the loaded set, falling back to ID when absent', () => {
    const parentTask = makeTask({ id: 'parent-1', title: 'Parent foundation task', status: 'COMPLETED' });
    const childTask = makeTask({ id: 'child-1', title: 'Child implementation task', status: 'IN_PROGRESS' });
    const task = makeTask({
      parent_task_id: 'parent-1',
      child_task_ids: ['child-1', 'child-missing-id'],
    });

    render(<DetailsTab task={task} tasks={[task, parentTask, childTask]} />);

    // Parent task: shows title and status badge
    expect(screen.getByText('Parent foundation task')).toBeInTheDocument();
    const parentStatus = screen.getByText('COMPLETED');
    expect(parentStatus.className).toContain('emerald');

    // Child task present in loaded set: shows title and color-coded status
    expect(screen.getByText('Child implementation task')).toBeInTheDocument();
    const childStatus = screen.getByText('IN_PROGRESS');
    expect(childStatus.className).toContain('amber');

    // Missing child: falls back to ID display
    expect(screen.getByText('child-missing-id')).toBeInTheDocument();
  });

  it('renders parent task as ID-only when not in the loaded task set', () => {
    const task = makeTask({ parent_task_id: 'unknown-parent-id' });
    render(<DetailsTab task={task} tasks={[task]} />);
    expect(screen.getByText('Parent Task')).toBeInTheDocument();
    expect(screen.getByText('unknown-parent-id')).toBeInTheDocument();
  });
});
