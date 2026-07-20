import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { describe, expect, test, vi } from 'vitest';
import ArchiveView from './ArchiveView';
import type { Task } from './TaskCard';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-archive-1',
    title: 'Archived task',
    description: 'Archived task details',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: '2026-06-30T10:00:00Z',
    requires_approval: false,
    created_by: 'tester',
    ...overrides,
  };
}

function renderArchive(overrides: Partial<ComponentProps<typeof ArchiveView>> = {}) {
  const archiveTasks = overrides.archiveTasks ?? [
    makeTask({ id: 'task-archive-1', title: 'Completed archive task', status: 'COMPLETED' }),
    makeTask({ id: 'task-archive-2', title: 'Failed archive task', status: 'FAILED' }),
  ];
  const props: ComponentProps<typeof ArchiveView> = {
    archiveTasks,
    archiveLoading: false,
    archivePage: 1,
    archivePageSize: 25,
    archiveStatusFilter: 'all',
    archiveSearchQuery: '',
    archiveDateRange: 'all',
    archiveTotalTasks: archiveTasks.length,
    setShowArchive: vi.fn(),
    setArchivePage: vi.fn(),
    setArchivePageSize: vi.fn(),
    setArchiveStatusFilter: vi.fn(),
    setArchiveSearchQuery: vi.fn(),
    setArchiveDateRange: vi.fn(),
    handleViewLogs: vi.fn(),
    onRetryTask: vi.fn(),
    onUnarchiveTask: vi.fn(),
    archiveSortOrder: 'desc',
    setArchiveSortOrder: vi.fn(),
    onPurgeArchive: vi.fn(async () => undefined),
    ...overrides,
  };

  render(<ArchiveView {...props} />);
  return props;
}

describe('ArchiveView', () => {
  test('routes filter, search, sort, and pagination controls through existing callbacks', async () => {
    const user = userEvent.setup();
    const setArchiveStatusFilter = vi.fn();
    const setArchiveDateRange = vi.fn();
    const setArchiveSearchQuery = vi.fn();
    const setArchiveSortOrder = vi.fn();
    const setArchivePageSize = vi.fn();
    const setArchivePage = vi.fn();
    renderArchive({
      archivePage: 2,
      archivePageSize: 25,
      archiveTotalTasks: 75,
      setArchiveStatusFilter,
      setArchiveDateRange,
      setArchiveSearchQuery,
      setArchiveSortOrder,
      setArchivePageSize,
      setArchivePage,
    });

    await user.click(screen.getByRole('button', { name: 'Failed' }));
    expect(setArchiveStatusFilter).toHaveBeenCalledWith('FAILED');

    await user.click(screen.getByRole('button', { name: '7d' }));
    expect(setArchiveDateRange).toHaveBeenCalledWith('7d');

    fireEvent.change(screen.getByRole('textbox', { name: 'Search archived tasks' }), {
      target: { value: 'runner' },
    });
    expect(setArchiveSearchQuery).toHaveBeenCalledWith('runner');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Archive sort order' }), 'asc');
    expect(setArchiveSortOrder).toHaveBeenCalledWith('asc');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Archived tasks per page' }), '50');
    expect(setArchivePageSize).toHaveBeenCalledWith(50);

    await user.click(screen.getByRole('button', { name: 'Previous' }));
    expect(setArchivePage).toHaveBeenCalledWith(1);
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(setArchivePage).toHaveBeenCalledWith(3);
  });

  test('opens purge options by click and confirms the selected purge range', async () => {
    const user = userEvent.setup();
    const onPurgeArchive = vi.fn(async () => undefined);
    renderArchive({ onPurgeArchive });

    const clearArchive = screen.getByRole('button', { name: /clear archive/i });
    expect(clearArchive).toHaveAttribute('aria-expanded', 'false');

    await user.click(clearArchive);
    expect(clearArchive).toHaveAttribute('aria-expanded', 'true');

    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menuitem', { name: 'Older than 90 days' })).not.toBeInTheDocument();

    await user.click(clearArchive);
    await user.click(screen.getByRole('menuitem', { name: 'Older than 90 days' }));
    expect(screen.getByRole('dialog', { name: 'Permanently delete?' })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    });

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Permanently delete?' })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(clearArchive).toHaveFocus();
    });

    await user.click(clearArchive);
    await user.click(screen.getByRole('menuitem', { name: 'Older than 90 days' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onPurgeArchive).not.toHaveBeenCalled();

    await user.click(clearArchive);
    await user.click(screen.getByRole('menuitem', { name: 'Older than 90 days' }));

    await user.click(screen.getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() => {
      expect(onPurgeArchive).toHaveBeenCalledWith(90);
    });
  });

  test('keeps purge dialog visible while destructive purge is pending', async () => {
    const user = userEvent.setup();
    let resolvePurge!: () => void;
    const onPurgeArchive = vi.fn(
      () => new Promise<void>((resolve) => {
        resolvePurge = resolve;
      }),
    );
    renderArchive({ onPurgeArchive });

    const clearArchive = screen.getByRole('button', { name: /clear archive/i });
    await user.click(clearArchive);
    await user.click(screen.getByRole('menuitem', { name: 'Older than 90 days' }));
    await user.click(screen.getByRole('button', { name: 'Delete permanently' }));

    expect(onPurgeArchive).toHaveBeenCalledWith(90);
    expect(screen.getByRole('dialog', { name: 'Permanently delete?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    const deleteButton = screen.getByRole('button', { name: 'Delete permanently' });
    expect(deleteButton).toHaveAttribute('aria-disabled', 'true');
    expect(deleteButton).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: 'Permanently delete?' })).toBeInTheDocument();
    await user.tab();
    expect(deleteButton).toHaveFocus();

    resolvePurge();
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Permanently delete?' })).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(clearArchive).toHaveFocus();
    });
  });

  test('row actions call details, retry, and unarchive handlers', async () => {
    const user = userEvent.setup();
    const onUnarchiveTask = vi.fn();
    const onRetryTask = vi.fn();
    const handleViewLogs = vi.fn();
    renderArchive({ onUnarchiveTask, onRetryTask, handleViewLogs });

    await user.click(screen.getAllByRole('button', { name: 'Details' })[0]);
    expect(handleViewLogs).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-archive-1' }));

    await user.click(screen.getAllByRole('button', { name: 'Unarchive' })[0]);
    expect(onUnarchiveTask).toHaveBeenCalledWith('task-archive-1');

    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetryTask).toHaveBeenCalledTimes(1);
    expect(onRetryTask).toHaveBeenCalledWith('task-archive-2');
  });

  test('empty state clears filters through existing callbacks', async () => {
    const user = userEvent.setup();
    const setArchiveStatusFilter = vi.fn();
    const setArchiveSearchQuery = vi.fn();
    const setArchiveDateRange = vi.fn();
    renderArchive({
      archiveTasks: [],
      archiveTotalTasks: 0,
      archiveStatusFilter: 'FAILED',
      archiveSearchQuery: 'runner',
      archiveDateRange: '30d',
      setArchiveStatusFilter,
      setArchiveSearchQuery,
      setArchiveDateRange,
    });

    await user.click(within(screen.getByTestId('archive-empty-state')).getByRole('button', { name: 'Clear filters' }));

    expect(setArchiveStatusFilter).toHaveBeenCalledWith('all');
    expect(setArchiveSearchQuery).toHaveBeenCalledWith('');
    expect(setArchiveDateRange).toHaveBeenCalledWith('all');
  });
});
