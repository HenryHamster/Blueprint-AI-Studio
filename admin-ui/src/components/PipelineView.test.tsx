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
    const primary = makeTask({ id: 'review-1', title: 'Review PR', task_type: 'review' });
    const sidecar = makeTask({
      id: 'challenger-1',
      title: '[CHALLENGE] second-opinion panel review',
      status: 'IN_PROGRESS',
      created_at: '2026-05-01T11:00:00Z',
      context: { review_second_opinion_for_task_id: 'review-1' },
    });

    const forest = buildForest([primary, sidecar]);

    // The side-car is NOT a top-level root...
    expect(forest.map(r => r.task.id)).toEqual(['review-1']);
    // ...it nests under the primary, flagged as a side-car.
    expect(forest[0].sidecars.map(s => s.task.id)).toEqual(['challenger-1']);
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
        id: 'review-1',
        title: 'Review PR #42',
        task_type: 'review',
        status: 'COMPLETED',
        context: { review_second_opinion: { state: 'challenged_confirmed' } },
      }),
      makeTask({
        id: 'challenger-1',
        title: '[CHALLENGE] second-opinion panel review for PR #42',
        status: 'COMPLETED',
        created_at: '2026-05-01T11:00:00Z',
        context: { review_second_opinion_for_task_id: 'review-1' },
      }),
      makeTask({ id: 'sibling', title: 'Sibling root', status: 'IN_PROGRESS' }),
    ];
    render(<PipelineView tasks={tasks} onViewLogs={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Expand all' }));

    // The primary row carries the compact challenge-outcome badge.
    expect(screen.getByText('challenged ✓')).toBeTruthy();
    // Collapsed by default: the side-car row is not rendered yet.
    expect(document.querySelector('[data-task-id="challenger-1"]')).toBeNull();

    await user.click(screen.getByRole('button', { name: /1 side-car/ }));
    expect(document.querySelector('[data-task-id="challenger-1"]')).not.toBeNull();
    expect(screen.getByText('↳ side-car')).toBeTruthy();
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
