import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { SrBadge } from './SrBadges';
import type { Task } from '../types/task';

function makeTask(context: Record<string, unknown>): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    context,
  } as Task;
}

describe('SrBadge tone differentiation', () => {
  it('pass-fixed renders with teal coloring, distinct from pass emerald', () => {
    const passTask = makeTask({
      _self_review_outcome: { verdict: 'pass', fixes_applied: false, check_results: { correctness: { passed: true } } },
    });
    const passFixedTask = makeTask({
      _self_review_outcome: { verdict: 'pass', fixes_applied: true, check_results: { correctness: { passed: false } } },
    });

    const { container: passContainer } = render(<SrBadge task={passTask} />);
    const { container: fixedContainer } = render(<SrBadge task={passFixedTask} />);

    const passEl = passContainer.firstElementChild as HTMLElement;
    const fixedEl = fixedContainer.firstElementChild as HTMLElement;

    // pass uses success (emerald) token classes
    expect(passEl.className).toContain('status-success');
    expect(passEl.className).not.toContain('teal');

    // pass-fixed uses teal — visually distinct to signal self-correction
    expect(fixedEl.className).toContain('teal');
    expect(fixedEl.className).not.toContain('status-success');
  });
});
