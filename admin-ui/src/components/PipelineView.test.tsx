import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PipelineView } from './PipelineView';
import type { Task } from './TaskCard';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-x',
    title: 'Task X',
    description: '',
    status: 'QUEUED',
    task_type: 'create',
    created_by: 'USER',
    requires_approval: false,
    created_at: '2026-05-01T10:00:00Z',
    ...overrides,
  };
}

/** A task node is dimmed (filtered out) when it carries the opacity/grayscale class. */
function nodeFor(taskId: string): HTMLElement {
  const el = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!el) throw new Error(`no node for ${taskId}`);
  return el as HTMLElement;
}

describe('PipelineView Blocked filter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('matches first-class BLOCKED tasks, not just QUEUED+depends_on', async () => {
    const user = userEvent.setup();
    const tasks = [
      makeTask({ id: 'blocked-status', title: 'Blocked by status', status: 'BLOCKED' }),
      makeTask({ id: 'legacy-blocked', title: 'Legacy blocked', status: 'QUEUED', depends_on: ['blocked-status'] }),
      makeTask({ id: 'plain-queued', title: 'Plain queued', status: 'QUEUED' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);

    // Root groups default-collapse when they contain no active/failed work;
    // expand them so the task nodes render before asserting on dimming.
    await user.click(screen.getByRole('button', { name: 'Expand all' }));
    await user.click(screen.getByRole('button', { name: 'Blocked' }));

    // Real BLOCKED status and the legacy QUEUED+deps inference both stay lit.
    expect(nodeFor('blocked-status').className).not.toMatch(/opacity-40/);
    expect(nodeFor('legacy-blocked').className).not.toMatch(/opacity-40/);
    // A plain QUEUED task with no deps is dimmed under the Blocked filter.
    expect(nodeFor('plain-queued').className).toMatch(/opacity-40/);
  });
});
