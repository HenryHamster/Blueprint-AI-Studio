import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { Task } from '../types/task';
import type { Alert } from '../types/alert';
import type { Direction } from './DirectionsPanel';
import {
  StrategicCockpit,
  deriveStrategicCockpitModel,
  extractTaskDirectionId,
  getJudgmentReasons,
} from './StrategicCockpit';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Implement operator cockpit',
    description: 'Build the cockpit surface',
    status: 'QUEUED',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-06-27T10:00:00',
    ...overrides,
  };
}

function makeDirection(overrides: Partial<Direction> = {}): Direction {
  return {
    id: 'dir-1',
    text: 'Improve release health',
    priority: 'high',
    added_at: '2026-06-20T10:00:00',
    status: 'active',
    ...overrides,
  };
}

function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 'alert-1',
    project_id: 'proj-1',
    severity: 'WARNING',
    title: 'Direction needs attention',
    message: 'A workstream has stalled.',
    task_id: null,
    created_at: '2026-06-27T12:00:00',
    dismissed_at: null,
    ...overrides,
  };
}

describe('StrategicCockpit helpers', () => {
  it('derives cockpit counts from task status and judgment signals', () => {
    const tasks = [
      makeTask({ id: 'active', status: 'IN_PROGRESS', title: 'Active task' }),
      makeTask({ id: 'queued', status: 'QUEUED', title: 'Queued task' }),
      makeTask({ id: 'eval', status: 'EVALUATING', requires_approval: true, title: 'Review task' }),
      makeTask({ id: 'blocked', status: 'BLOCKED', title: 'Blocked task' }),
      makeTask({ id: 'parked', status: 'PARKED', title: 'Parked task' }),
      makeTask({ id: 'failed', status: 'FAILED', title: 'Failed task' }),
      makeTask({ id: 'done', status: 'COMPLETED', title: 'Done task', pr_url: 'https://github.com/acme/repo/pull/42' }),
    ];

    const model = deriveStrategicCockpitModel(tasks);

    expect(model.counts.total).toBe(7);
    expect(model.counts.active).toBe(2);
    expect(model.counts.queued).toBe(1);
    expect(model.counts.needsJudgment).toBe(3);
    expect(model.counts.friction).toBe(3);
    expect(model.counts.completed).toBe(1);
    expect(model.evidence.map(item => item.label)).toContain('Pull request');
  });

  it('extracts direction metadata and groups tasks into direction workstreams', () => {
    const task = makeTask({
      id: 'task-dir',
      task_type: 'research',
      context: { direction_id: 'dir-1' },
    });
    const direction = makeDirection();

    const model = deriveStrategicCockpitModel([task], [direction]);

    expect(extractTaskDirectionId(task)).toBe('dir-1');
    expect(model.workstreams).toHaveLength(1);
    expect(model.workstreams[0].label).toBe('Improve release health');
    expect(model.workstreams[0].kind).toBe('direction');
  });

  it('keeps active directions visible even when no task is currently attached', () => {
    const direction = makeDirection({
      id: 'dir-empty',
      text: 'Shape operator workflows around Strategic conversation',
    });

    const model = deriveStrategicCockpitModel([], [direction]);

    expect(model.workstreams).toHaveLength(1);
    expect(model.workstreams[0]).toMatchObject({
      key: 'direction:dir-empty',
      label: 'Shape operator workflows around Strategic conversation',
      total: 0,
      kind: 'direction',
    });
  });

  it('falls back to a research finding direction when it is unambiguous', () => {
    const task = makeTask({
      research_findings: [
        {
          title: 'Finding A',
          description: 'A',
          prompt: 'A',
          scope: 'small',
          risk: 'low',
          priority: 'high',
          related_files: [],
          direction_id: 'dir-research',
        },
      ],
    });

    expect(extractTaskDirectionId(task)).toBe('dir-research');
  });

  it('deduplicates judgment reasons and ignores terminal approval flags', () => {
    expect(getJudgmentReasons(makeTask({ status: 'EVALUATING', requires_approval: true }))).toEqual([
      'approval',
      'evaluating',
    ]);
    expect(getJudgmentReasons(makeTask({ status: 'COMPLETED', requires_approval: true }))).toEqual([]);
  });

  it('orders active alerts before dismissed alerts and lower severity', () => {
    const model = deriveStrategicCockpitModel([], [], [
      makeAlert({ id: 'dismissed', severity: 'ERROR', dismissed_at: '2026-06-27T12:01:00' }),
      makeAlert({ id: 'info', severity: 'INFO', created_at: '2026-06-27T12:03:00' }),
      makeAlert({ id: 'error', severity: 'ERROR', created_at: '2026-06-27T12:02:00' }),
    ]);

    expect(model.alertQueue.map(alert => alert.id)).toEqual(['error', 'info']);
  });
});

describe('StrategicCockpit', () => {
  it('renders the core cockpit sections and opens task rows', () => {
    const onViewTask = vi.fn();
    const tasks = [
      makeTask({
        id: 'review-task',
        title: 'Review launch gates',
        status: 'EVALUATING',
        requires_approval: true,
        task_type: 'review',
      }),
      makeTask({
        id: 'completed-task',
        title: 'Ship release evidence',
        status: 'COMPLETED',
        task_type: 'create',
        pr_url: 'https://github.com/acme/repo/pull/42',
      }),
    ];

    render(
      <StrategicCockpit
        tasks={tasks}
        directions={[makeDirection()]}
        alerts={[makeAlert()]}
        selectedProjectId="proj-1"
        onViewTask={onViewTask}
      />,
    );

    expect(screen.getByText('Strategic Cockpit')).toBeInTheDocument();
    expect(screen.getByText('Mission Cards')).toBeInTheDocument();
    expect(screen.getByText('Workday Flow')).toBeInTheDocument();
    expect(screen.getByText('Judgment Queue')).toBeInTheDocument();
    expect(screen.getByText('Evidence Bundle')).toBeInTheDocument();
    expect(screen.getByText('Friction Watch')).toBeInTheDocument();

    const taskButton = screen
      .getAllByRole('button', { name: /Review launch gates/i })
      .find(button => button.textContent?.includes('review-t'));
    if (!taskButton) throw new Error('Expected clickable review task row');

    fireEvent.click(taskButton);
    expect(onViewTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'review-task' }));
  });

  it('renders direction backlog entries even when task-heavy streams fill mission cards', () => {
    const tasks = Array.from({ length: 7 }, (_, index) => makeTask({
      id: `project-task-${index}`,
      title: `Active project stream ${index}`,
      status: 'IN_PROGRESS',
      project_id: `project-${index}`,
    }));

    render(
      <StrategicCockpit
        tasks={tasks}
        directions={[
          makeDirection({
            id: 'dir-backlog',
            text: 'Shape operator workflows around Strategic conversation',
          }),
        ]}
        selectedProjectId="proj-1"
      />,
    );

    expect(screen.getByText('Direction Backlog')).toBeInTheDocument();
    expect(screen.getByText('Shape operator workflows around Strategic conversation')).toBeInTheDocument();
  });

  it('prioritizes active direction backlog entries over inactive zero-task directions', () => {
    render(
      <StrategicCockpit
        tasks={[]}
        directions={[
          makeDirection({ id: 'dir-completed-a', text: 'A completed archive direction', status: 'completed' }),
          makeDirection({ id: 'dir-expired-b', text: 'B expired direction', status: 'expired' }),
          makeDirection({ id: 'dir-paused-c', text: 'C paused direction', status: 'paused' }),
          makeDirection({ id: 'dir-paused-d', text: 'D paused direction', status: 'paused' }),
          makeDirection({ id: 'dir-proposed-e', text: 'E proposed direction', status: 'proposed' }),
          makeDirection({ id: 'dir-staged-f', text: 'F staged direction', status: 'staged' }),
          makeDirection({ id: 'dir-paused-g', text: 'G paused direction', status: 'paused' }),
          makeDirection({ id: 'dir-active-z', text: 'Z active direction stays visible', status: 'active' }),
        ]}
        selectedProjectId="proj-1"
      />,
    );

    expect(screen.getByText('Direction Backlog')).toBeInTheDocument();
    expect(screen.getByText('Z active direction stays visible')).toBeInTheDocument();
    expect(screen.queryByText('A completed archive direction')).not.toBeInTheDocument();
    expect(screen.queryByText('B expired direction')).not.toBeInTheDocument();
  });
});
