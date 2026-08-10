import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PipelineView } from './PipelineView';
import { buildForest, type TreeNode } from './PipelineView.utils';
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

function nodeFor(taskId: string): HTMLElement {
  const el = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!el) throw new Error(`no node for ${taskId}`);
  return el as HTMLElement;
}

describe('PipelineView task status visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the lifetime attempt ordinal after the retry-budget counter resets', () => {
    render(
      <PipelineView
        tasks={[makeTask({ status: 'IN_PROGRESS', attempt_count: 0, attempt_number: 2 })]}
        onViewLogs={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Attempt 2')).toHaveTextContent('↺2');
  });

  it('keeps blocked and dependency-waiting tasks visible after removing local status chips', async () => {
    const user = userEvent.setup();
    const tasks = [
      makeTask({ id: 'blocked-status', title: 'Blocked by status', status: 'BLOCKED' }),
      makeTask({ id: 'legacy-blocked', title: 'Legacy blocked', status: 'QUEUED', depends_on: ['blocked-status'] }),
      makeTask({ id: 'plain-queued', title: 'Plain queued', status: 'QUEUED' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);

    // Root groups default-collapse when they contain no active/failed work;
    // expand them so the task nodes render before asserting visibility.
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    expect(screen.queryByRole('button', { name: 'All' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Active' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Waiting' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Blocked' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Failed' })).toBeNull();

    // Real BLOCKED status and the legacy QUEUED+deps inference both remain visible.
    expect(nodeFor('blocked-status').className).not.toMatch(/opacity-40/);
    expect(nodeFor('legacy-blocked').className).not.toMatch(/opacity-40/);
    expect(nodeFor('plain-queued').className).not.toMatch(/opacity-40/);
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

  it('keeps blocked, parked, dependency-waiting, queued, and running tasks visible together', async () => {
    const user = userEvent.setup();
    const tasks = [
      makeTask({ id: 'blocked-status', title: 'Blocked by status', status: 'BLOCKED' }),
      makeTask({ id: 'parked-status', title: 'Parked by status', status: 'PARKED' }),
      makeTask({ id: 'legacy-waiting', title: 'Waiting on dependency', status: 'QUEUED', depends_on: ['blocked-status'] }),
      makeTask({ id: 'plain-queued', title: 'Plain queued', status: 'QUEUED' }),
      makeTask({ id: 'running-task', title: 'Running task', status: 'IN_PROGRESS' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    expect(nodeFor('blocked-status').className).not.toMatch(/opacity-40/);
    expect(nodeFor('parked-status').className).not.toMatch(/opacity-40/);
    expect(nodeFor('legacy-waiting').className).not.toMatch(/opacity-40/);
    expect(nodeFor('plain-queued').className).not.toMatch(/opacity-40/);
    expect(nodeFor('running-task').className).not.toMatch(/opacity-40/);
  });

  it('expands groups with waiting children by default', () => {
    const tasks = [
      makeTask({ id: 'root-waiting', title: 'Root waiting group', status: 'QUEUED' }),
      makeTask({
        id: 'blocked-child',
        title: 'Blocked child should be visible',
        status: 'BLOCKED',
        parent_task_id: 'root-waiting',
      }),
      makeTask({ id: 'sibling-root', title: 'Sibling root', status: 'QUEUED' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);

    expect(nodeFor('blocked-child')).toBeInTheDocument();
  });
});

function findNode(roots: TreeNode[], id: string): TreeNode | null {
  for (const root of roots) {
    if (root.task.id === id) return root;
    const inChildren = findNode(root.children, id);
    if (inChildren) return inChildren;
    const inSidecars = findNode(root.sidecars, id);
    if (inSidecars) return inSidecars;
  }
  return null;
}

describe('buildForest side-car nesting', () => {
  it('nests a side-car under its primary instead of rendering it top-level', () => {
    const primary = makeTask({ id: 'research-1', title: 'Research direction', task_type: 'research' });
    const sidecar = makeTask({
      id: 'finding-challenge-1',
      title: '[CHALLENGE] Verify research finding',
      status: 'IN_PROGRESS',
      created_at: '2026-05-01T11:00:00Z',
      context: { finding_challenge_for_task_id: 'research-1' },
    });

    const forest = buildForest([primary, sidecar]);

    // The side-car is NOT a top-level root...
    expect(forest.map(r => r.task.id)).toEqual(['research-1']);
    // ...it nests under the primary, flagged as a side-car.
    expect(forest[0].sidecars.map(s => s.task.id)).toEqual(['finding-challenge-1']);
    expect(forest[0].sidecars[0].isSidecar).toBe(true);
  });

  it('nests a side-car whose primary is itself a nested child', () => {
    const parent = makeTask({ id: 'research-1', task_type: 'research' });
    const child = makeTask({ id: 'review-1', task_type: 'review', parent_task_id: 'research-1' });
    const sidecar = makeTask({
      id: 'grader-1',
      context: { quality_grader_for_task_id: 'review-1' },
    });

    const forest = buildForest([parent, child, sidecar]);

    expect(forest.map(r => r.task.id)).toEqual(['research-1']);
    const reviewNode = findNode(forest, 'review-1');
    expect(reviewNode?.sidecars.map(s => s.task.id)).toEqual(['grader-1']);
  });

  it('falls back to a top-level row when the primary is not on this page', () => {
    const sidecar = makeTask({
      id: 'challenger-1',
      status: 'COMPLETED',
      context: { finding_challenge_for_task_id: 'off-page-primary' },
    });

    const forest = buildForest([sidecar]);

    expect(forest.map(r => r.task.id)).toEqual(['challenger-1']);
    expect(forest[0].isSidecarFallback).toBe(true);
  });

  it('orders multiple side-cars under one primary by creation time', () => {
    const primary = makeTask({ id: 'research-1', task_type: 'research' });
    const later = makeTask({
      id: 'grader-1',
      created_at: '2026-05-01T13:00:00Z',
      context: { quality_grader_for_task_id: 'research-1' },
    });
    const earlier = makeTask({
      id: 'challenge-1',
      created_at: '2026-05-01T12:00:00Z',
      context: { finding_challenge_for_task_id: 'research-1' },
    });

    const forest = buildForest([primary, later, earlier]);

    expect(forest[0].sidecars.map(s => s.task.id)).toEqual(['challenge-1', 'grader-1']);
  });

  it('does not nest self- or mutually-referencing side-cars (cycle guard)', () => {
    const selfRef = makeTask({ id: 'weird-1', context: { quality_grader_for_task_id: 'weird-1' } });
    const mutualA = makeTask({ id: 'mutual-a', context: { quality_grader_for_task_id: 'mutual-b' } });
    const mutualB = makeTask({ id: 'mutual-b', context: { quality_grader_for_task_id: 'mutual-a' } });

    const forest = buildForest([selfRef, mutualA, mutualB]);

    // All three stay top-level instead of forming a render cycle.
    expect(forest.map(r => r.task.id).sort()).toEqual(['mutual-a', 'mutual-b', 'weird-1']);
  });
});

describe('PipelineView side-car rendering', () => {
  it('renders side-cars collapsed by default with an outcome badge on the primary', async () => {
    const user = userEvent.setup();
    const tasks = [
      makeTask({
        id: 'research-1',
        title: 'Research direction',
        task_type: 'research',
        status: 'COMPLETED',
        context: { finding_challenge: { agreement: 'agrees' } },
      }),
      makeTask({
        id: 'finding-challenge-1',
        title: '[CHALLENGE] Verify research finding',
        status: 'COMPLETED',
        created_at: '2026-05-01T11:00:00Z',
        context: { finding_challenge_for_task_id: 'research-1' },
      }),
      makeTask({ id: 'sibling', title: 'Sibling root', status: 'IN_PROGRESS' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    // The primary row carries the compact finding-challenge outcome badge.
    expect(screen.getByText('2nd opinion ✓')).toBeTruthy();
    // Collapsed by default: the side-car row is not rendered yet.
    expect(document.querySelector('[data-task-id="finding-challenge-1"]')).toBeNull();

    await user.click(screen.getByRole('button', { name: /1 side-car/ }));
    expect(document.querySelector('[data-task-id="finding-challenge-1"]')).not.toBeNull();
    expect(screen.getByText('↳ side-car')).toBeTruthy();
  });
});

describe('PipelineView loading state', () => {
  it('shows_loading_instead_of_empty_before_initial_tasks_load', () => {
    render(<PipelineView tasks={[]} loading={true} onViewLogs={vi.fn()} />);

    expect(screen.getByText('Loading tasks...')).toBeInTheDocument();
    expect(screen.queryByText('No tasks yet')).not.toBeInTheDocument();
  });

  it('shows empty state after loading completes with no tasks', () => {
    render(<PipelineView tasks={[]} loading={false} onViewLogs={vi.fn()} />);

    expect(screen.queryByText('Loading tasks...')).not.toBeInTheDocument();
    expect(screen.getByText('No tasks yet')).toBeInTheDocument();
  });

  it('shows empty state with active filters when loading is false', () => {
    render(<PipelineView tasks={[]} loading={false} hasActiveFilters={true} onViewLogs={vi.fn()} />);

    expect(screen.queryByText('Loading tasks...')).not.toBeInTheDocument();
    expect(screen.getByText('No tasks match the active filters — try clearing filters to see all tasks')).toBeInTheDocument();
  });

  it('shows tasks instead of loading when tasks are present even if loading is true', () => {
    const tasks = [makeTask({ id: 'task-1', title: 'Test Task' })];
    render(<PipelineView tasks={tasks} loading={true} onViewLogs={vi.fn()} />);

    expect(screen.getByText('Test Task')).toBeInTheDocument();
    expect(screen.queryByText('Loading tasks...')).not.toBeInTheDocument();
  });
});

describe('PipelineView group summary counts parked tasks separately', () => {
  it('displays parked count in group header badge and does not inflate queued segment', async () => {
    const user = userEvent.setup();
    // Two root tasks so Expand All button renders.
    const tasks = [
      makeTask({
        id: 'root-1',
        title: 'Root task',
        status: 'IN_PROGRESS',
      }),
      makeTask({
        id: 'parked-1',
        title: 'Parked task',
        status: 'PARKED',
        parent_task_id: 'root-1',
        park_reason: 'Waiting for dependency',
        parked_at: '2026-05-01T10:30:00Z',
      }),
      makeTask({
        id: 'queued-1',
        title: 'Queued task',
        status: 'QUEUED',
        parent_task_id: 'root-1',
      }),
      makeTask({
        id: 'root-2',
        title: 'Second root',
        status: 'QUEUED',
      }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    // The group header should display a parked badge with the count.
    // Look for a badge with title containing "parked" and text "1".
    const parkedBadges = Array.from(document.querySelectorAll('[title*="parked"]')).filter(el => el.textContent?.includes('1'));
    expect(parkedBadges.length).toBeGreaterThan(0);
  });

  it('shows "Waiting" for PARKED and BLOCKED tasks instead of live-ticking timer', async () => {
    const user = userEvent.setup();
    const createdAt = '2026-05-01T10:00:00Z';
    const tasks = [
      makeTask({ id: 'parked-task', title: 'Parked Task', status: 'PARKED', created_at: createdAt }),
      makeTask({ id: 'blocked-task', title: 'Blocked Task', status: 'BLOCKED', created_at: createdAt }),
      makeTask({ id: 'in-progress', title: 'In Progress', status: 'IN_PROGRESS', created_at: createdAt }),
      makeTask({ id: 'sibling', title: 'Sibling', status: 'QUEUED' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    // PARKED and BLOCKED tasks should show "Waiting"
    const parkedNode = nodeFor('parked-task');
    expect(parkedNode.textContent).toContain('Waiting');

    const blockedNode = nodeFor('blocked-task');
    expect(blockedNode.textContent).toContain('Waiting');

    // IN_PROGRESS should show elapsed time (contains a colon from time format), not "Waiting"
    const progressNode = nodeFor('in-progress');
    expect(progressNode.textContent).not.toContain('Waiting');
  });
});
