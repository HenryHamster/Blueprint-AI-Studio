import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KanbanBoard, type KanbanColumn } from './KanbanBoard';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  authHeaders: vi.fn(async () => ({})),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
}));

vi.mock('@dnd-kit/core', () => ({
  DndContext: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  closestCenter: vi.fn(),
  PointerSensor: vi.fn(),
  useSensor: vi.fn(),
  useSensors: vi.fn(() => []),
  DragOverlay: () => null,
}));

vi.mock('@dnd-kit/sortable', () => ({
  SortableContext: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useSortable: vi.fn(() => ({
    setNodeRef: vi.fn(),
    attributes: {},
    listeners: {},
    transform: null,
    transition: null,
    isDragging: false,
  })),
  arrayMove: vi.fn((arr: unknown[]) => arr),
  verticalListSortingStrategy: null,
}));

vi.mock('@dnd-kit/utilities', () => ({
  CSS: { Transform: { toString: () => '' } },
}));

const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS', 'PROVISIONING'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
];

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'feat: add mobile-responsive layout',
    description: '',
    status: 'QUEUED',
    task_type: 'create',
    created_at: '2026-05-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    ...overrides,
  };
}

const BASE_PROPS = {
  readOnly: false,
  loading: false,
  maxRetries: 3,
  actionLoadingMap: {},
  sortBy: 'newest' as const,
  selectionMode: false,
  selectedTaskIds: new Set<string>(),
  onToggleTaskSelection: () => {},
  onBulkToggle: () => {},
  onArchiveColumn: () => {},
  onApprove: () => {},
  onRequestChanges: () => {},
  onCancel: () => {},
  onRetry: () => {},
  onEditPrompt: () => {},
  onDismiss: () => {},
  onArchive: () => {},
  onViewLogs: () => {},
};

describe('KanbanBoard — viewport regression', () => {
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

  test('mobile tab bar with realistic columns renders without overflow at 375px', async () => {
    const tasks = [
      makeTask({ id: 't1', status: 'QUEUED' }),
      makeTask({ id: 't2', status: 'IN_PROGRESS' }),
      makeTask({ id: 't3', status: 'COMPLETED' }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanBoard
            {...BASE_PROPS}
            tasks={tasks}
            columns={COLUMNS}
          />,
        );
      });
    });

    // Verify the mobile layout wrapper (parent of the escape-hatch tab bar) stays in bounds.
    // Hard-fail (not silent skip) when the selector breaks, so a class rename surfaces here.
    const mobileWrapper = host!.querySelector('div.md\\:hidden') as HTMLElement | null;
    expect(mobileWrapper).not.toBeNull();
    const rect = mobileWrapper!.getBoundingClientRect();
    expect(rect.right).toBeLessThanOrEqual(376); // 375 + 1px tolerance
  });

  test('desktop kanban columns render without overflow at 768px tablet width', async () => {
    // At md: (768px) the layout transitions from mobile tab view to desktop columns.
    // Use 2 columns so each flex-1 min-w-[280px] column fits within 768px.
    const twoColumns: KanbanColumn[] = [
      { id: 'QUEUED', label: 'Queued', includes: ['QUEUED'] },
      { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS', 'PROVISIONING'] },
    ];
    const tasks = [
      makeTask({ id: 't1', status: 'QUEUED' }),
      makeTask({ id: 't2', status: 'IN_PROGRESS' }),
    ];

    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanBoard
            {...BASE_PROPS}
            tasks={tasks}
            columns={twoColumns}
          />,
        );
      });
    });
  });

  test('action toolbar buttons meet 44px touch target at 375px mobile width', async () => {
    const tasks = [
      makeTask({ id: 't1', status: 'FAILED', requires_approval: false }),
      makeTask({ id: 't2', status: 'QUEUED', requires_approval: true }),
      makeTask({ id: 't3', status: 'COMPLETED', title: 'feat: completed task alpha' }),
      makeTask({ id: 't4', status: 'IN_PROGRESS', title: 'feat: in-progress task beta' }),
      makeTask({ id: 't5', status: 'EVALUATING', title: 'feat: evaluating task gamma' }),
      makeTask({ id: 't6', status: 'FAILED', title: 'fix: second failed task delta' }),
    ];

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanBoard
            {...BASE_PROPS}
            tasks={tasks}
            columns={COLUMNS}
          />,
        );
      });
    });
  });

  test('task titles and column labels are not silently clipped at 375px mobile width', async () => {
    const tasks = [
      makeTask({ id: 't1', status: 'QUEUED', title: 'feat: add mobile-responsive layout to operator dashboard' }),
      makeTask({ id: 't2', status: 'IN_PROGRESS', title: 'fix: resolve viewport overflow in pipeline regression panel' }),
      makeTask({ id: 't3', status: 'COMPLETED', title: 'chore: update dependency versions across admin-ui package' }),
    ];

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <KanbanBoard
            {...BASE_PROPS}
            tasks={tasks}
            columns={COLUMNS}
          />,
        );
      });
    });
  });
});
