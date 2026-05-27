import { describe, test, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TimelineView } from './TimelineView';
import type { Task } from '../types/task';

vi.mock('lucide-react', () => ({
  ChevronDown: () => null,
  ChevronRight: () => null,
}));

function makeTask(id: string, overrides: Partial<Task> = {}): Task {
  const now = Date.now();
  return {
    id,
    title: `Task ${id}`,
    description: '',
    status: 'IN_PROGRESS',
    created_by: 'agent',
    requires_approval: false,
    created_at: new Date(now - 2 * 3600 * 1000).toISOString(),
    started_at: new Date(now - 1.5 * 3600 * 1000).toISOString(),
    last_heartbeat: new Date(now - 60 * 1000).toISOString(),
    ...overrides,
  };
}

describe('TimelineView', () => {
  test('shows empty state when no tasks provided', () => {
    render(<TimelineView tasks={[]} hasActiveFilters={false} onViewLogs={vi.fn()} />);
    expect(screen.getByText('No tasks in the selected time window')).toBeTruthy();
  });

  test('renders task title in sidebar for tasks within the window', () => {
    const tasks = [makeTask('t1', { title: 'Build the feature', task_type: undefined })];
    render(<TimelineView tasks={tasks} hasActiveFilters={false} onViewLogs={vi.fn()} />);
    expect(screen.getByTitle('Build the feature')).toBeTruthy();
  });

  test('calls onViewLogs when a task sidebar row is clicked', async () => {
    const onViewLogs = vi.fn();
    const tasks = [makeTask('t1', { title: 'Clickable task', task_type: undefined })];
    render(<TimelineView tasks={tasks} hasActiveFilters={false} onViewLogs={onViewLogs} />);
    await userEvent.click(screen.getByTitle('Clickable task'));
    expect(onViewLogs).toHaveBeenCalledWith(tasks[0]);
  });

  test('shows active filters indicator when hasActiveFilters is true', () => {
    const tasks = [makeTask('t1')];
    render(<TimelineView tasks={tasks} hasActiveFilters={true} onViewLogs={vi.fn()} />);
    expect(screen.getByText('Filters active')).toBeTruthy();
  });

  test('renders time window selector buttons', () => {
    render(<TimelineView tasks={[]} hasActiveFilters={false} onViewLogs={vi.fn()} />);
    expect(screen.getByText('1h')).toBeTruthy();
    expect(screen.getByText('6h')).toBeTruthy();
    expect(screen.getByText('24h')).toBeTruthy();
  });
});
