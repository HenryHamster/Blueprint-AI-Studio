import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { CatchUpInProgressSection, type ActiveTask } from './CatchUpInProgressSection';

const baseTask = (overrides: Partial<ActiveTask> = {}): ActiveTask => ({
  task_id: 't1',
  title: 'Refactor something',
  status: 'running',
  task_type: 'refactor',
  started_at: '2026-05-29T08:00:00Z',
  ...overrides,
});

describe('CatchUpInProgressSection', () => {
  it('renders nothing when activeCount is 0', () => {
    const { container } = render(
      <CatchUpInProgressSection
        activeCount={0}
        activeTasks={[]}
        expanded
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders the toggle button with the active count', () => {
    render(
      <CatchUpInProgressSection
        activeCount={3}
        activeTasks={[baseTask()]}
        expanded={false}
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(screen.getByRole('button', { name: /in progress \(3\)/i })).toBeInTheDocument();
  });

  it('hides task rows when collapsed', () => {
    render(
      <CatchUpInProgressSection
        activeCount={1}
        activeTasks={[baseTask({ title: 'Hidden row' })]}
        expanded={false}
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(screen.queryByText('Hidden row')).not.toBeInTheDocument();
  });

  it('renders one row per active task when expanded', () => {
    render(
      <CatchUpInProgressSection
        activeCount={2}
        activeTasks={[
          baseTask({ task_id: 'a', title: 'Task A' }),
          baseTask({ task_id: 'b', title: 'Task B' }),
        ]}
        expanded
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(screen.getByText('Task A')).toBeInTheDocument();
    expect(screen.getByText('Task B')).toBeInTheDocument();
  });

  it('calls onToggle when the header button is clicked', () => {
    const onToggle = vi.fn();
    render(
      <CatchUpInProgressSection
        activeCount={1}
        activeTasks={[baseTask()]}
        expanded={false}
        onToggle={onToggle}
        onSelectTask={() => {}}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /in progress/i }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('calls onSelectTask with the task id on row click', () => {
    const onSelectTask = vi.fn();
    render(
      <CatchUpInProgressSection
        activeCount={1}
        activeTasks={[baseTask({ task_id: 'xyz', title: 'Clickable' })]}
        expanded
        onToggle={() => {}}
        onSelectTask={onSelectTask}
      />
    );
    fireEvent.click(screen.getByText('Clickable'));
    expect(onSelectTask).toHaveBeenCalledWith({ id: 'xyz' });
  });

  it('calls onSelectTask on Enter and Space keydown', () => {
    const onSelectTask = vi.fn();
    render(
      <CatchUpInProgressSection
        activeCount={1}
        activeTasks={[baseTask({ task_id: 'kbd', title: 'Keyboard row' })]}
        expanded
        onToggle={() => {}}
        onSelectTask={onSelectTask}
      />
    );
    const row = screen.getByText('Keyboard row').closest('[role="button"]');
    expect(row).not.toBeNull();
    fireEvent.keyDown(row as Element, { key: 'Enter' });
    fireEvent.keyDown(row as Element, { key: ' ' });
    expect(onSelectTask).toHaveBeenCalledTimes(2);
    expect(onSelectTask).toHaveBeenCalledWith({ id: 'kbd' });
  });

  it('renders "+N more" when activeCount exceeds the rendered tasks', () => {
    render(
      <CatchUpInProgressSection
        activeCount={5}
        activeTasks={[baseTask({ task_id: 'a' }), baseTask({ task_id: 'b' })]}
        expanded
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(screen.getByText('+3 more')).toBeInTheDocument();
  });

  it('renders direction, scope, review-iteration and cost badges conditionally', () => {
    render(
      <CatchUpInProgressSection
        activeCount={1}
        activeTasks={[
          baseTask({
            direction_text: 'Decompose the largest admin-ui panels into smaller seams',
            scope: 'medium',
            review_iteration: 2,
            review_cap: 3,
            running_cost_usd: 1.25,
          }),
        ]}
        expanded
        onToggle={() => {}}
        onSelectTask={() => {}}
      />
    );
    expect(screen.getByText('medium')).toBeInTheDocument();
    expect(screen.getByText('2/3 iter')).toBeInTheDocument();
    expect(screen.getByText('1.25')).toBeInTheDocument();
  });
});
