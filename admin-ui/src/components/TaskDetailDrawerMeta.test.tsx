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
});
