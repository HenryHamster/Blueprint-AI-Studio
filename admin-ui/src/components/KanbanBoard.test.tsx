import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { KanbanBoard, type KanbanColumn } from './KanbanBoard';
import type { Task } from '../types/task';

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

vi.mock('./TaskCard', () => ({
  TaskCard: ({ task }: { task: Task }) => (
    <div data-testid="task-card">{task.title}</div>
  ),
  TASK_TYPE_STYLES: {},
}));

const testColumns: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS', 'PROVISIONING'] },
  { id: 'BLOCKED', label: 'Blocked', includes: ['BLOCKED'] },
  { id: 'PARKED', label: 'Parked', includes: ['PARKED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
];

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: '',
    status: 'QUEUED',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    ...overrides,
  };
}

function makeProps(overrides: Record<string, unknown> = {}) {
  return {
    tasks: [] as Task[],
    columns: testColumns,
    loading: false,
    maxRetries: 3,
    actionLoadingMap: {} as Record<string, string>,
    selectionMode: false,
    selectedTaskIds: new Set<string>(),
    sortBy: 'newest' as const,
    onToggleTaskSelection: vi.fn(),
    onBulkToggle: vi.fn(),
    onArchiveColumn: vi.fn(),
    onApprove: vi.fn(),
    onRequestChanges: vi.fn(),
    onCancel: vi.fn(),
    onRetry: vi.fn(),
    onEditPrompt: vi.fn(),
    onDismiss: vi.fn(),
    onArchive: vi.fn(),
    onViewLogs: vi.fn(),
    ...overrides,
  };
}

describe('KanbanBoard', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn().mockReturnValue(null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders all column labels', () => {
    render(<KanbanBoard {...makeProps()} />);
    expect(screen.getAllByText('Queued').length).toBeGreaterThan(0);
    expect(screen.getAllByText('In Progress').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Blocked').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Parked').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0);
  });

  it('renders BLOCKED tasks in the Blocked column', () => {
    const task = makeTask({ id: 'b1', title: 'Blocked Task', status: 'BLOCKED' });
    render(<KanbanBoard {...makeProps({ tasks: [task] })} />);
    expect(screen.getAllByText('Blocked Task').length).toBeGreaterThan(0);
  });

  it('does not offer a column-archive button for the non-terminal Blocked column', () => {
    // BLOCKED is a waiting state, not terminal — it must not be bulk-archivable
    // the way COMPLETED/CANCELLED/FAILED are.
    const task = makeTask({ id: 'b1', title: 'Blocked Task', status: 'BLOCKED' });
    render(<KanbanBoard {...makeProps({ tasks: [task] })} />);
    expect(screen.queryByTitle('Archive all Blocked tasks')).not.toBeInTheDocument();
  });

  it('renders PARKED tasks in the Parked column without a column-archive button', () => {
    const task = makeTask({ id: 'p1', title: 'Parked Task', status: 'PARKED' });
    render(<KanbanBoard {...makeProps({ tasks: [task] })} />);
    expect(screen.getAllByText('Parked Task').length).toBeGreaterThan(0);
    expect(screen.queryByTitle('Archive all Parked tasks')).not.toBeInTheDocument();
  });

  it('renders tasks with their titles', () => {
    const tasks = [
      makeTask({ id: '1', title: 'Queued Task', status: 'QUEUED' }),
      makeTask({ id: '2', title: 'Active Task', status: 'IN_PROGRESS' }),
    ];
    render(<KanbanBoard {...makeProps({ tasks })} />);
    expect(screen.getAllByText('Queued Task').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Active Task').length).toBeGreaterThan(0);
  });

  it('does not show empty state when loading=true', () => {
    render(<KanbanBoard {...makeProps({ loading: true })} />);
    expect(screen.queryByText('No tasks')).not.toBeInTheDocument();
  });

  it('shows empty state when loading=false and columns are empty', () => {
    render(<KanbanBoard {...makeProps({ loading: false })} />);
    expect(screen.getAllByText('No tasks').length).toBeGreaterThan(0);
  });

  it('column collapse toggle hides expanded content', async () => {
    const user = userEvent.setup();
    const task = makeTask({ title: 'Queued Task', status: 'QUEUED' });
    render(<KanbanBoard {...makeProps({ tasks: [task] })} />);

    const collapseBtn = screen.getByTitle('Collapse Queued');
    // The expanded content div is the ancestor containing "flex-col flex-1" in its class
    const expandedContent = collapseBtn.closest('[class*="flex-col flex-1"]');
    expect(expandedContent).not.toHaveClass('hidden');

    await user.click(collapseBtn);
    expect(expandedContent).toHaveClass('hidden');
  });

  it('shows checkboxes for tasks when selectionMode=true', () => {
    const tasks = [makeTask({ id: '1', title: 'Task One', status: 'QUEUED' })];
    render(<KanbanBoard {...makeProps({ tasks, selectionMode: true })} />);
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes.length).toBeGreaterThan(0);
  });

  it('archive button appears for terminal columns with tasks and fires handler on second click', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    const task = makeTask({ id: '1', title: 'Done Task', status: 'COMPLETED' });
    render(<KanbanBoard {...makeProps({ tasks: [task], onArchiveColumn })} />);

    const archiveBtns = screen.getAllByTitle('Archive all Completed tasks');
    expect(archiveBtns.length).toBeGreaterThan(0);
    // First click enters confirm state — must NOT fire yet
    await user.click(archiveBtns[0]);
    expect(onArchiveColumn).not.toHaveBeenCalled();
    // Second click fires the handler
    const confirmBtns = screen.getAllByTitle('Archive 1 tasks? (4s)');
    expect(confirmBtns.length).toBeGreaterThan(0);
    await user.click(confirmBtns[0]);
    expect(onArchiveColumn).toHaveBeenCalledWith(['1'], 'Completed');
  });
});
