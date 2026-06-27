import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BulkActionBar } from './BulkActionBar';
import type { Task } from '../types/task';

function makeTask(id: string, status: Task['status'], title: string): Task {
  return {
    id,
    title,
    description: '',
    status,
    created_by: 'user-1',
    requires_approval: status === 'EVALUATING',
    created_at: '2026-05-06T00:00:00Z',
  };
}

const baseHandlers = {
  handleBulkAction: vi.fn(),
  handleBulkCancel: vi.fn(),
  handleBulkArchiveColumn: vi.fn(),
  toggleSelectionMode: vi.fn(),
};

describe('BulkActionBar confirm-dialog consequence copy', () => {
  it('bulk cancel dialog spells out VM termination and discarded work', async () => {
    const user = userEvent.setup();
    const setConfirmDialog = vi.fn();
    const cancellableTasks = [makeTask('t1', 'IN_PROGRESS', 'Task One')];
    render(
      <BulkActionBar
        selectedCount={1}
        approvableTasks={[]}
        cancellableTasks={cancellableTasks}
        failedTasks={[]}
        terminalTasks={[]}
        setConfirmDialog={setConfirmDialog}
        {...baseHandlers}
      />,
    );
    await user.click(screen.getByText('Cancel 1'));
    expect(setConfirmDialog).toHaveBeenCalledTimes(1);
    const dialog = setConfirmDialog.mock.calls[0][0];
    expect(dialog.message).toContain('terminates their VMs');
    expect(dialog.message).toContain('In-progress work is discarded');
    // The affected-task list is still included below the consequence line.
    expect(dialog.message).toContain('Task One');
  });

  it('bulk archive dialog clarifies it is recoverable', async () => {
    const user = userEvent.setup();
    const setConfirmDialog = vi.fn();
    const terminalTasks = [makeTask('t9', 'COMPLETED', 'Done Task')];
    render(
      <BulkActionBar
        selectedCount={1}
        approvableTasks={[]}
        cancellableTasks={[]}
        failedTasks={[]}
        terminalTasks={terminalTasks}
        setConfirmDialog={setConfirmDialog}
        {...baseHandlers}
      />,
    );
    await user.click(screen.getByText('Archive 1'));
    const dialog = setConfirmDialog.mock.calls[0][0];
    expect(dialog.message).toContain('Recoverable via Unarchive');
    expect(dialog.message).toContain('Done Task');
  });

  it('bulk retry dialog keeps its plain task list (no destructive prefix)', async () => {
    const user = userEvent.setup();
    const setConfirmDialog = vi.fn();
    const failedTasks = [makeTask('t5', 'FAILED', 'Broken Task')];
    render(
      <BulkActionBar
        selectedCount={1}
        approvableTasks={[]}
        cancellableTasks={[]}
        failedTasks={failedTasks}
        terminalTasks={[]}
        setConfirmDialog={setConfirmDialog}
        {...baseHandlers}
      />,
    );
    await user.click(screen.getByText('Retry 1'));
    const dialog = setConfirmDialog.mock.calls[0][0];
    expect(dialog.message).toBe('Broken Task');
  });
});
