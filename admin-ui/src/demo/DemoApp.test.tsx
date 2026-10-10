import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DemoApp } from './DemoApp';
import { initialTasks, transition } from './model';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('portfolio demo', () => {
  it('queues a new prompt locally and supports keyboard navigation in the detail tabs', () => {
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'New sample task' }));
    expect(
      screen.getByRole('button', { name: 'Queue sample task' }),
    ).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: 'Task title' }), {
      target: { value: 'Add a health endpoint' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: 'Goal' }), {
      target: { value: 'Return readiness and cover failure cases.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Queue sample task' }));
    expect(
      screen.getByRole('heading', { name: 'Add a health endpoint' }),
    ).toBeInTheDocument();
    const detail = screen.getByRole('article', { name: 'Selected task' });
    expect(within(detail).getByText('Queued')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Overview' }), {
      key: 'ArrowRight',
    });
    expect(screen.getByRole('tab', { name: 'Execution log' })).toHaveFocus();
    expect(screen.getByRole('tabpanel')).toHaveTextContent('BP-106 accepted');
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Execution log' }), {
      key: 'End',
    });
    expect(screen.getByRole('tab', { name: /Changes/ })).toHaveFocus();
  });

  it('keeps approval separate from execution and does not bypass a failed worker', () => {
    const running = { ...initialTasks[0] };
    expect(transition(running, 'approve')).toEqual(running);
    const failed = transition(running, 'fail');
    expect(transition(failed, 'advance')).toEqual(failed);
    expect(transition(failed, 'approve')).toEqual(failed);
    const retried = transition(failed, 'retry');
    expect(retried).toMatchObject({ status: 'QUEUED', attempt: 2 });
    let task = retried;
    for (let i = 0; i < 5; i++) task = transition(task, 'advance');
    expect(task.status).toBe('EVALUATING');
    expect(transition(task, 'approve').status).toBe('COMPLETED');
  });

  it('plays to the review boundary, requires approval, and labels the simulation', () => {
    vi.useFakeTimers();
    render(<DemoApp />);
    expect(
      screen.getByText('Sample data. Execution and checks are simulated.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Replay a task' }));
    for (let i = 0; i < 3; i++)
      act(() => {
        vi.advanceTimersByTime(1300);
      });
    expect(
      screen.getByRole('button', { name: 'Approve sample' }),
    ).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(
      screen.getByRole('button', { name: 'Approve sample' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve sample' }));
    expect(
      screen.getByText('Task complete. No deployment performed.'),
    ).toBeInTheDocument();
  });

  it('interrupts a worker, retries with a new attempt, and pauses cleanly', () => {
    vi.useFakeTimers();
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Interrupt worker' }));
    expect(
      screen.getByText('Worker interrupted. Retry to recover.'),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Next step' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }));
    expect(screen.getByText('Attempt 2')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    const detail = screen.getByRole('article', { name: 'Selected task' });
    expect(within(detail).getByText('Queued')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next step' }));
    expect(within(detail).getByText('Preparing')).toBeInTheDocument();
  });

  it('searches tasks, switches selection without advancing the old task, and navigates views', () => {
    vi.useFakeTimers();
    render(<DemoApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Replay a task' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Search tasks' }), {
      target: { value: 'Security' },
    });
    const task = screen.getByRole('button', { name: /BP-102/ });
    fireEvent.click(task);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(
      screen.getByText('Task complete. No deployment performed.'),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: 'Search tasks' }), {
      target: { value: 'no-such-task' },
    });
    expect(
      screen.getByText('No tasks match “no-such-task”.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Fleet' }));
    expect(
      screen.getByRole('heading', { name: 'Runner fleet' }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Architecture' }));
    expect(
      screen.getByRole('heading', { name: 'What this demo proves' }),
    ).toBeInTheDocument();
  });
});
