import { describe, it, expect } from 'vitest';
import { getFailureClassBreakdown } from './failureClass';
import type { Task } from '../types/task';

describe('getFailureClassBreakdown', () => {
  it('returns empty object for empty task list', () => {
    expect(getFailureClassBreakdown([])).toEqual({});
  });

  it('counts tasks by failure class', () => {
    const tasks: Task[] = [
      { id: '1', title: 'T1', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'CODE_ERROR' } as Task,
      { id: '2', title: 'T2', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'CODE_ERROR' } as Task,
      { id: '3', title: 'T3', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'AGENT_TIMEOUT' } as Task,
      { id: '4', title: 'T4', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'PROVISION_FAILURE' } as Task,
    ];
    expect(getFailureClassBreakdown(tasks)).toEqual({
      CODE_ERROR: 2,
      AGENT_TIMEOUT: 1,
      PROVISION_FAILURE: 1,
    });
  });

  it('ignores tasks without failure class', () => {
    const tasks: Task[] = [
      { id: '1', title: 'T1', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'CODE_ERROR' } as Task,
      { id: '2', title: 'T2', description: '', status: 'COMPLETED', created_by: '', requires_approval: false, failure_class: undefined } as Task,
      { id: '3', title: 'T3', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: null } as Task,
    ];
    expect(getFailureClassBreakdown(tasks)).toEqual({
      CODE_ERROR: 1,
    });
  });

  it('handles unknown failure classes', () => {
    const tasks: Task[] = [
      { id: '1', title: 'T1', description: '', status: 'FAILED', created_by: '', requires_approval: false, failure_class: 'UNKNOWN_CLASS' } as Task,
    ];
    expect(getFailureClassBreakdown(tasks)).toEqual({
      UNKNOWN_CLASS: 1,
    });
  });
});
