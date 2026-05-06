import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KanbanBoard, type KanbanColumn } from './KanbanBoard';
import type { Task } from '../types/task';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  authHeaders: vi.fn(async () => ({})),
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
  });

  test('action toolbar buttons are touch-accessible (44px) at 375px mobile width', async () => {
    const tasks = [
      makeTask({ id: 't1', status: 'FAILED', requires_approval: false }),
      makeTask({ id: 't2', status: 'QUEUED', requires_approval: true }),
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
  });
});
