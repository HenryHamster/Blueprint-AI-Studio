import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KanbanColumn } from './KanbanColumn';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';
import type { Task } from './TaskCard';

vi.mock('../api', () => ({
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-viewport-1',
    title: 'feat(viewport): add KanbanColumn 44px touch targets test at 375px',
    description: 'Add viewport test for KanbanColumn to validate touch targets',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'blueprint-agent',
    requires_approval: false,
    created_at: new Date(Date.now() - 3600_000).toISOString(),
    started_at: new Date(Date.now() - 1800_000).toISOString(),
    last_heartbeat: new Date(Date.now() - 30_000).toISOString(),
    latest_status_message: 'Running viewport tests',
    ...overrides,
  };
}

const noop = () => {};

describe('KanbanColumn — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders expanded column without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanColumn
            readOnly={false}
            col={{ id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] }}
            colIdx={0}
            columnTasks={[makeTask()]}
            loading={false}
            isCollapsed={false}
            sortBy="newest"
            compact={false}
            selectionMode={false}
            selectedTaskIds={new Set()}
            blockingMap={{}}
            maxRetries={5}
            actionLoadingMap={{}}
            onBulkToggle={noop}
            onApprove={noop}
            onRequestChanges={noop}
            onCancel={noop}
            onRetry={noop}
            onEditPrompt={noop}
            onDismiss={noop}
            onArchive={noop}
            onArchiveColumn={noop}
            onViewLogs={noop}
            onToggleTaskSelection={noop}
            toggleCollapse={noop}
            handleResizeStart={noop}
            showResizeHandle={false}
            getCardRef={() => noop}
            handleNavigate={noop}
          />
        );
      });
    });
  });

  test('renders collapsed column without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanColumn
            readOnly={false}
            col={{ id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] }}
            colIdx={1}
            columnTasks={[makeTask({ status: 'COMPLETED' })]}
            loading={false}
            isCollapsed={true}
            sortBy="newest"
            compact={false}
            selectionMode={false}
            selectedTaskIds={new Set()}
            blockingMap={{}}
            maxRetries={5}
            actionLoadingMap={{}}
            onBulkToggle={noop}
            onApprove={noop}
            onRequestChanges={noop}
            onCancel={noop}
            onRetry={noop}
            onEditPrompt={noop}
            onDismiss={noop}
            onArchive={noop}
            onArchiveColumn={noop}
            onViewLogs={noop}
            onToggleTaskSelection={noop}
            toggleCollapse={noop}
            handleResizeStart={noop}
            showResizeHandle={false}
            getCardRef={() => noop}
            handleNavigate={noop}
          />
        );
      });
    });
  });

  test('archivable column buttons meet 44px touch target or have allow-small-target attribute at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanColumn
            readOnly={false}
            col={{ id: 'FAILED', label: 'Failed', includes: ['FAILED'] }}
            colIdx={2}
            columnTasks={[makeTask({ status: 'FAILED' })]}
            loading={false}
            isCollapsed={false}
            sortBy="newest"
            compact={false}
            selectionMode={false}
            selectedTaskIds={new Set()}
            blockingMap={{}}
            maxRetries={5}
            actionLoadingMap={{}}
            onBulkToggle={noop}
            onApprove={noop}
            onRequestChanges={noop}
            onCancel={noop}
            onRetry={noop}
            onEditPrompt={noop}
            onDismiss={noop}
            onArchive={noop}
            onArchiveColumn={noop}
            onViewLogs={noop}
            onToggleTaskSelection={noop}
            toggleCollapse={noop}
            handleResizeStart={noop}
            showResizeHandle={false}
            getCardRef={() => noop}
            handleNavigate={noop}
          />
        );
      });
    });
  });
});
