import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { ComponentProps } from 'react';
import { TaskDetailDrawer as TaskDetailDrawerComponent } from './TaskDetailDrawer';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

type TaskDetailDrawerViewportProps = Omit<ComponentProps<typeof TaskDetailDrawerComponent>, 'canOperateProject'> & {
  canOperateProject?: boolean;
};

function TaskDetailDrawer(props: TaskDetailDrawerViewportProps) {
  return <TaskDetailDrawerComponent canOperateProject={true} {...props} />;
}

// Stub firebase before any module that transitively imports it.
// Browser-mode vitest validates ESM exports statically, so the mock must
// expose every name `../firebase` actually exports (api.ts pulls in
// getIdToken; other call sites use onAuthChange).
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: 'A test description',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    ...overrides,
  };
}

describe('TaskDetailDrawer — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask()}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('long task title and description are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask({
              title: 'feat(regression): classify viewport-test failures as a distinct regression class with per-layer escalation_reason metadata attached to each pipeline case',
              description: 'fix(backend): handle the edge case where pipeline_regression_dispatcher emits duplicate escalation_reason labels on retry loops — add dedup guard in dispatch_single_signal and update test_pipeline_regression_dispatcher.py to cover the retry path',
              latest_status_message: 'backend/testing/test_pipeline_regression_dispatcher.py — 47 tests passing; admin-ui/src/components/PipelineRegressionsPanel.viewport.test.tsx — building at 375px mobile viewport',
            })}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('header buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask()}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    }, { selector: '[title="Fullscreen (f)"], [title*="Exit fullscreen"]' });
  });

  test('all drawer buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask()}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('approval footer renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask({ requires_approval: true })}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('failed task footer renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask({ status: 'FAILED' })}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet width)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask()}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('long task title and description are not silently clipped at 768px', async () => {
    await expectNoTextTruncationAt(768, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask({
              title: 'feat(regression): classify viewport-test failures as a distinct regression class with per-layer escalation_reason metadata attached to each pipeline case',
              description: 'fix(backend): handle the edge case where pipeline_regression_dispatcher emits duplicate escalation_reason labels on retry loops — add dedup guard in dispatch_single_signal and update test_pipeline_regression_dispatcher.py to cover the retry path',
              latest_status_message: 'backend/testing/test_pipeline_regression_dispatcher.py — 47 tests passing; admin-ui/src/components/PipelineRegressionsPanel.viewport.test.tsx — building at 768px tablet viewport',
            })}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 1280px (desktop width)', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskDetailDrawer
            task={makeTask()}
            logLines={[]}
            onClose={() => {}}
            onApprove={() => {}}
            onRequestChanges={() => {}}
            onCancel={() => {}}
            onRetry={() => {}}
            onEditPrompt={() => {}}
            onDismiss={() => {}}
          />,
        );
      });
    });
  });
});
