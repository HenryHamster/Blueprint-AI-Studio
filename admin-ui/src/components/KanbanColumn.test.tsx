import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ColumnArchiveButton } from './KanbanColumn';

vi.mock('../api', () => ({
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
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
  TaskCard: () => <div data-testid="task-card" />,
  TASK_TYPE_STYLES: {},
}));

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('ColumnArchiveButton', () => {
  it('first click does NOT call onArchiveColumn', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    render(
      <ColumnArchiveButton
        taskIds={['1', '2', '3']}
        columnLabel="Completed"
        onArchiveColumn={onArchiveColumn}
      />
    );
    await user.click(screen.getByTitle('Archive all Completed tasks'));
    expect(onArchiveColumn).not.toHaveBeenCalled();
  });

  it('first click shows countdown label with task count', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    render(
      <ColumnArchiveButton
        taskIds={['1', '2', '3']}
        columnLabel="Completed"
        onArchiveColumn={onArchiveColumn}
      />
    );
    await user.click(screen.getByTitle('Archive all Completed tasks'));
    expect(screen.getByTitle('Archive 3 tasks? (4s)')).toBeInTheDocument();
  });

  it('second click within window calls onArchiveColumn with correct args', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    render(
      <ColumnArchiveButton
        taskIds={['a', 'b', 'c']}
        columnLabel="Failed"
        onArchiveColumn={onArchiveColumn}
      />
    );
    await user.click(screen.getByTitle('Archive all Failed tasks'));
    await user.click(screen.getByTitle('Archive 3 tasks? (4s)'));
    expect(onArchiveColumn).toHaveBeenCalledWith(['a', 'b', 'c'], 'Failed');
    expect(onArchiveColumn).toHaveBeenCalledTimes(1);
  });

  it('countdown expires without second click — button resets, onArchiveColumn not called', () => {
    vi.useFakeTimers();
    try {
      const onArchiveColumn = vi.fn();
      render(
        <ColumnArchiveButton
          taskIds={['1']}
          columnLabel="Cancelled"
          onArchiveColumn={onArchiveColumn}
        />
      );
      act(() => { fireEvent.click(screen.getByTitle('Archive all Cancelled tasks')); });
      expect(screen.getByTitle('Archive 1 tasks? (4s)')).toBeInTheDocument();

      // Advance past the 4-second window
      act(() => { vi.advanceTimersByTime(4000); });

      // Should be back to normal
      expect(screen.getByTitle('Archive all Cancelled tasks')).toBeInTheDocument();
      expect(onArchiveColumn).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('second click on >10-task column shows window.confirm and fires when confirmed', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    vi.stubGlobal('confirm', vi.fn(() => true));
    const ids = Array.from({ length: 11 }, (_, i) => String(i));
    render(
      <ColumnArchiveButton
        taskIds={ids}
        columnLabel="Completed"
        onArchiveColumn={onArchiveColumn}
      />
    );
    await user.click(screen.getByTitle('Archive all Completed tasks'));
    await user.click(screen.getByTitle('Archive 11 tasks? (4s)'));
    expect(window.confirm).toHaveBeenCalledWith(
      'Archive all 11 Completed tasks? This cannot be undone.'
    );
    expect(onArchiveColumn).toHaveBeenCalledWith(ids, 'Completed');
  });

  it('second click on >10-task column does NOT fire when confirm is cancelled', async () => {
    const user = userEvent.setup();
    const onArchiveColumn = vi.fn();
    vi.stubGlobal('confirm', vi.fn(() => false));
    const ids = Array.from({ length: 11 }, (_, i) => String(i));
    render(
      <ColumnArchiveButton
        taskIds={ids}
        columnLabel="Failed"
        onArchiveColumn={onArchiveColumn}
      />
    );
    await user.click(screen.getByTitle('Archive all Failed tasks'));
    await user.click(screen.getByTitle('Archive 11 tasks? (4s)'));
    expect(onArchiveColumn).not.toHaveBeenCalled();
  });
});
