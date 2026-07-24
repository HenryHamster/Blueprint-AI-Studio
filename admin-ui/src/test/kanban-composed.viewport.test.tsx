import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { KanbanBoard, type KanbanColumn } from '../components/KanbanBoard';
import type { Task } from '../types/task';
import { expectNoOverflowAt } from './viewport';

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

const healthBarProps = {
  health: {
    last_cycle_seconds_ago: 90,
    queued_tasks: 3,
    active_tasks: 1,
    pool_info: {},
    deploy_state: {
      status: 'OK',
      rollback_count: 0,
      rollback_reason: null,
      pause_reason: null,
      monitoring_since: null,
      recent_outcomes: [],
    },
    deploy_monitoring: undefined,
  },
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: 1746576000000,
  tasks: [],
  pipelineThrottle: null,
};

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

const kanbanBaseProps = {
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

describe('KanbanBoard composed with shell panels — viewport regression', () => {
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

  test('no horizontal overflow at 375px', async () => {
    const tasks = [
      makeTask({ id: 't1', status: 'QUEUED', title: 'feat: implement kanban board mobile layout' }),
      makeTask({ id: 't2', status: 'IN_PROGRESS', title: 'fix: resolve overflow in task card' }),
      makeTask({ id: 't3', status: 'EVALUATING', title: 'research: analyze pipeline performance' }),
      makeTask({ id: 't4', status: 'COMPLETED', title: 'chore: update dependency versions' }),
      makeTask({ id: 't5', status: 'FAILED', title: 'fix: correct viewport overflow in metrics panel' }),
      makeTask({ id: 't6', status: 'IN_PROGRESS', title: 'feat: add structured output handler' }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <div className="flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen">
            <OrchestratorHealthBar {...healthBarProps} />
            <div className="h-10 flex items-center border-b border-neutral-800 shrink-0" aria-label="navigation" />
            <KanbanBoard
              {...kanbanBaseProps}
              tasks={tasks}
              columns={COLUMNS}
            />
          </div>,
        );
      });
    }, { waitMs: 75 });

    expect(host!.textContent).toContain('Queued');
  });
});
