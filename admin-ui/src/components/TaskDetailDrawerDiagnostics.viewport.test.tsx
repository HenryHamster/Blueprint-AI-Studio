import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskDiagnosticsPanel } from './TaskDetailDrawerDiagnostics';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const fetchSerialMock = vi.fn<(...args: unknown[]) => Promise<{ serial_log: string; source: string; vm_status: string; vm_instance_id: string }>>(async () => ({ serial_log: 'live serial output', source: 'live', vm_status: 'RUNNING', vm_instance_id: 'test' }));

vi.mock('../api', () => ({
  fetchTaskSerialConsole: (...args: unknown[]) => fetchSerialMock(...args),
}));

// FAILED status opens the panel by default (no click needed).
const diagTask: Task = {
  id: 'task-550e8400e29b41d4a716446655440000',
  title: 'Failed Task with Diagnostics and Long Identifiers',
  description: 'A test description',
  status: 'FAILED',
  task_type: 'create',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'USER',
  requires_approval: false,
  vm_instance_id: 'blueprint-agent-c99e9c2f7a9e8b1c2d3e4f5a6b7c8d9e-0123',
  status_log: [
    { t: '2024-01-01T00:00:01Z', msg: 'Status changed: QUEUED -> IN_PROGRESS' },
    { t: '2024-01-01T00:01:01Z', msg: 'Provisioning failed on zone us-central1-a' },
  ],
  context: {
    last_serial_log: 'serial console line 1\nserial console line 2\nserial console line 3',
    _tool_trace: [
      { tool: 'Bash', ts: '2024-01-01T00:00:05Z', command: 'npm run build' },
      { name: 'Edit', ts: '2024-01-01T00:00:06Z', file_path: 'src/app.ts' },
    ],
    retry_after: '2024-01-01T00:05:00Z',
    provision_zone: 'us-central1-a',
    provision_failures: 3,
    max_plan_account_id: 'acct-550e8400e29b41d4a716446655440000',
    blacklisted_accounts: ['acct-aaa', 'acct-bbb'],
    _first_attempt_succeeded: false,
  },
};

// COMPLETED + non-stale: panel stays collapsed, exposing only the header
// control (which opts out via data-viewport-allow-small-target). Mirrors the
// original DetailsTab touch-target gate, which never expanded the diagnostics
// panel for its COMPLETED fixture.
const collapsedTask: Task = { ...diagTask, status: 'COMPLETED' };

describe('TaskDetailDrawerDiagnostics TaskDiagnosticsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    fetchSerialMock.mockClear();
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders expanded panel at 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<TaskDiagnosticsPanel task={diagTask} />); });
    });
  });

  test('does not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<TaskDiagnosticsPanel task={diagTask} />); });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    // The collapse header opts out via data-viewport-allow-small-target; the
    // remaining controls (copy / fetch / more) are pre-existing dense drawer
    // markup unchanged by this extraction.
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<TaskDiagnosticsPanel task={collapsedTask} />); });
    }, { selector: 'button, a[href]' });
  });

  test('renders serial console, status log, tool trace, and scalar fields', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<TaskDiagnosticsPanel task={diagTask} />); });

    const content = host.textContent ?? '';
    expect(content).toContain('Serial console');
    expect(content).toContain('serial console line 1');
    expect(content).toContain('Recent status log');
    expect(content).toContain('Tool trace');
    expect(content).toContain('Scalar fields');
    expect(content).toContain('Provision zone');
    expect(content).toContain('us-central1-a');
  });

  test('Fetch current serial button fires fetchTaskSerialConsole with the task id', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<TaskDiagnosticsPanel task={diagTask} />); });

    const buttons = host.querySelectorAll('button');
    const fetchBtn = Array.from(buttons).find(b => b.textContent?.includes('Fetch current serial'));
    expect(fetchBtn).toBeTruthy();
    await act(async () => { fetchBtn?.click(); });

    expect(fetchSerialMock).toHaveBeenCalledTimes(1);
    expect(fetchSerialMock.mock.calls[0][0]).toBe(diagTask.id);
  });
});
