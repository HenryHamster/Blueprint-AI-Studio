import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FailureDetailPanel } from './TaskDetailDrawerFailureDetail';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// Far-future retry_after so the "Auto-retrying in Nm" branch renders deterministically.
const failedTask: Task = {
  id: 'task-550e8400e29b41d4a716446655440000',
  title: 'Failed Task with a Failure Class and Long Identifiers',
  description: 'A test description',
  status: 'FAILED',
  task_type: 'create',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'USER',
  requires_approval: false,
  failure_class: 'CODE_ERROR',
  context: {
    retry_after: '2099-01-01T00:00:00Z',
  },
};

describe('TaskDetailDrawerFailureDetail FailureDetailPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders failure detail at 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<FailureDetailPanel task={failedTask} />); });
    });
  });

  test('does not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<FailureDetailPanel task={failedTask} />); });
    });
  });

  test('renders badge label, suggestion, and auto-retry ETA', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<FailureDetailPanel task={failedTask} />); });

    const content = host.textContent ?? '';
    expect(content).toContain('Code Error');
    expect(content).toContain('Review error logs before retrying');
    expect(content).toContain('Auto-retrying in');
  });

  test('renders nothing for a non-failed task', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<FailureDetailPanel task={{ ...failedTask, status: 'COMPLETED' }} />); });
    expect(host.textContent ?? '').toBe('');
  });
});
