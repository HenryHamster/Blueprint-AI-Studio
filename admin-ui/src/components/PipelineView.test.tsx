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

  it('surfaces blocker_kind/blocked_reason/recheck_after in the BLOCKED node tooltip', async () => {
    const user = userEvent.setup();
    // Two root tasks so the "Expand all" toolbar button renders (forest.length > 1).
    const tasks = [
      makeTask({
        id: 'blocked-meta',
        title: 'Blocked with metadata',
        status: 'BLOCKED',
        blocker_kind: 'missing_prerequisite',
        blocked_reason: 'Waiting on schema migration to land',
        recheck_after: '2026-05-02T10:00:00Z',
      }),
      makeTask({ id: 'sibling', title: 'Sibling root', status: 'IN_PROGRESS' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    const title = nodeFor('blocked-meta').getAttribute('title') ?? '';
    expect(title).toContain('missing_prerequisite');
    expect(title).toContain('Waiting on schema migration to land');
    expect(title).toMatch(/Rechecks/);
  });

  it('omits the blocker tooltip lines for a non-BLOCKED task', async () => {
    const user = userEvent.setup();
    const tasks = [
      makeTask({
        id: 'plain-running',
        title: 'Running task',
        status: 'IN_PROGRESS',
        // Stale blocker metadata on a non-BLOCKED task must not leak into the tooltip.
        blocked_reason: 'stale reason that should not render',
      }),
      makeTask({ id: 'sibling', title: 'Sibling root', status: 'QUEUED' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    const title = nodeFor('plain-running').getAttribute('title') ?? '';
    expect(title).not.toContain('stale reason that should not render');
  });
});
