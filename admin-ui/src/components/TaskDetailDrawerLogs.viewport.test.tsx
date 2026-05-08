import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { LogsTab } from './TaskDetailDrawerLogs';
import type { Task } from './TaskCard';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

const LONG_LINE =
  '/usr/local/lib/python3.11/site-packages/' +
  'very_long_package_name_that_just_keeps_going/'.repeat(8) +
  'module.py:42: RuntimeError: Connection timed out after 30000ms waiting for response from remote endpoint';

const baseTask: Task = {
  id: 'task-1',
  title: 'Test',
  description: '',
  status: 'COMPLETED',
  created_by: 'USER',
  requires_approval: false,
  created_at: '2024-01-01T00:00:00Z',
};

const logLines = [
  LONG_LINE,
  '[error] Some error occurred',
  '[Status: COMPLETED] Task finished successfully',
  'Regular log line with normal length content',
  'Another normal log line',
];

const phasedLogLines = [
  'Initializing task environment',
  'Cloning repository https://github.com/example/repo.git',
  'Clone completed successfully',
  'Running Claude to generate implementation',
  'Claude finished generating code',
  'Running tests to verify changes',
  '[Status: COMPLETED] Task finished successfully',
];

describe('TaskDetailDrawerLogs LogsTab — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with long log lines', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <LogsTab
            logLines={logLines}
            task={baseTask}
            logFilter=""
            setLogFilter={() => {}}
            logLevel="all"
            setLogLevel={() => {}}
            showLineNumbers={true}
            logsConnecting={false}
          />,
        );
      });
    });
  });

  test('renders phase-grouped logs without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <LogsTab
            logLines={phasedLogLines}
            task={baseTask}
            logFilter=""
            setLogFilter={() => {}}
            logLevel="all"
            setLogLevel={() => {}}
            showLineNumbers={false}
            logsConnecting={false}
          />,
        );
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <LogsTab
            logLines={logLines}
            task={baseTask}
            logFilter=""
            setLogFilter={() => {}}
            logLevel="all"
            setLogLevel={() => {}}
            showLineNumbers={true}
            logsConnecting={false}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });
});
