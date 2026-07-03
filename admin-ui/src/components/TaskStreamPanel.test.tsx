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

  test('distinguishes active tracked tasks with no events yet', () => {
    renderPanel();

    expect(screen.getByText('0 entries')).toBeInTheDocument();
    expect(screen.getByText('active, no events yet')).toBeInTheDocument();
    expect(screen.getByText('Waiting for events')).toBeInTheDocument();
  });
});
