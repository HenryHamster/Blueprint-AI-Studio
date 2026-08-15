import { afterEach, describe, expect, test, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import TaskStreamPanel from './TaskStreamPanel';
import type { Task, TrackLogEntry } from '../types/task';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-stream-1',
    project_id: 'proj-1',
    title: 'Tracked stream task',
    description: '',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'tester',
    requires_approval: false,
    created_at: '2026-06-30T10:00:00Z',
    ...overrides,
  };
}

function renderPanel({
  task = makeTask(),
  logs = [],
}: {
  task?: Task;
  logs?: TrackLogEntry[];
} = {}) {
  render(
    <TaskStreamPanel
      trackedTaskIds={new Set([task.id])}
      trackedLogs={new Map([[task.id, logs]])}
      tasks={[task]}
      onUntrack={vi.fn()}
      onUntrackAll={vi.fn()}
      variant="embedded"
    />,
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe('TaskStreamPanel', () => {
  test('shows last event freshness in the footer', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T12:01:00Z'));
    renderPanel({
      logs: [
        { id: 1, t: '2026-06-30T12:00:00Z', type: 'system', msg: 'started tracking' },
      ],
    });

    expect(screen.getByText('1 entries')).toBeInTheDocument();
    expect(screen.getByText('last event 1m ago')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(screen.getByText('last event 2m ago')).toBeInTheDocument();
  });

  test('labels a claimed task with no events as in progress', () => {
    renderPanel();

    expect(screen.getByText('0 entries')).toBeInTheDocument();
    expect(screen.getByText('in progress, no events yet')).toBeInTheDocument();
    expect(screen.getByText('Waiting for events')).toBeInTheDocument();
  });

  test.each([
    ['PROVISIONING', 'starting, no events yet'],
    ['EVALUATING', 'evaluating, no events yet'],
  ] as const)('does not call a %s task active before execution evidence', (status, label) => {
    renderPanel({ task: makeTask({ status }) });

    expect(screen.getByText(label)).toBeInTheDocument();
  });
});
