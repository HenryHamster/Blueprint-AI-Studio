import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskDetailDrawer } from './TaskDetailDrawer';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

// Stub firebase before any module that transitively imports it.
// Browser-mode vitest validates ESM exports statically, so the mock must
// expose every name `../firebase` actually exports (api.ts pulls in
// getIdToken; other call sites use onAuthChange).
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
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
});
