import type { Task } from '../types/task';

export interface FailureClassBreakdown {
  [failureClass: string]: number;
}

export function getFailureClassBreakdown(tasks: Task[]): FailureClassBreakdown {
  const breakdown: FailureClassBreakdown = {};
  for (const task of tasks) {
    if (task.failure_class) {
      breakdown[task.failure_class] = (breakdown[task.failure_class] ?? 0) + 1;
    }
  }
  return breakdown;
}
