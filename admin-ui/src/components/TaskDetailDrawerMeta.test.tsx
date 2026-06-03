import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { DetailsTab } from './TaskDetailDrawerMeta';
import type { Task } from '../types/task';

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

  it('renders the raw reason key when it is unmapped', () => {
    const task = makeTask({ unverified_merge_reason: 'some_future_reason' });
    render(<DetailsTab task={task} />);
    expect(screen.getByText('Merged unverified')).toBeInTheDocument();
    expect(screen.getByText(/some_future_reason/)).toBeInTheDocument();
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
});
