import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DetailsTab } from './TaskDetailDrawerMeta';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

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

const richTask: Task = {
  id: 'task-1',
  title: 'Test Task with Self-Review',
  description: 'A test description',
  status: 'COMPLETED',
  task_type: 'create',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'USER',
  requires_approval: false,
  context: {
    _self_review_outcome: {
      verdict: 'fail',
      fixes_applied: true,
      check_results: {
        'correctness': { passed: false, issue: 'Off-by-one in loop boundary calculation near line 42' },
        'type-safety': { passed: false, issue: 'Missing null check on optional field access' },
        'scope-drift': { passed: true },
        'style-conventions': { passed: true },
      },
      edges_considered: [
        { case: 'When the input contains a very long path like /usr/local/lib/python3.11/site-packages/some_deeply_nested_module/utils/helpers.py', handled: true, evidence: 'Path is split at OS separator' },
        { case: 'Edge case where the task context is null or undefined at the point of serialization', handled: false, blind_spot: 'No guard for undefined context fields' },
      ],
      reviewer_findings: [
        { category: 'correctness', severity: 'HIGH', description: 'The loop iterates past the end of the array when the input length is exactly a multiple of the batch size' },
        { category: 'error-handling', severity: 'MEDIUM', description: 'Unhandled promise rejection in the background sync path' },
      ],
    },
    _first_attempt_rejection_categories: {
      'correctness': { note: 'flagged by reviewer' },
      'type-safety': { note: 'missing types' },
      'documentation': {},
    },
  },
};

describe('TaskDetailDrawerMeta DetailsTab — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders DetailsTab with SelfReviewPanel expanded at 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={richTask} />); });

      // Expand the Self-Review panel
      const buttons = host.querySelectorAll('button');
      const srButton = Array.from(buttons).find(b => b.textContent?.includes('Self-Review'));
      if (srButton) {
        await act(async () => { srButton.click(); });
      }
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={richTask} />); });
    }, { selector: 'button, a[href]' });
  });
});
