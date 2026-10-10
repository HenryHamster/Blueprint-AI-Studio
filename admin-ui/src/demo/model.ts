import type { TaskStatus } from '../types/task';

export interface DemoTask {
  id: string;
  title: string;
  type: string;
  status: TaskStatus;
  description: string;
  branch: string;
  attempt: number;
}

export const initialTasks: readonly DemoTask[] = [
  {
    id: 'BP-104',
    title: 'Add cursor pagination to the activity feed',
    type: 'Feature',
    status: 'IN_PROGRESS',
    description:
      'Keep the activity feed responsive as a project grows. Add cursor-based pagination, preserve chronological ordering, and cover the boundary cases with tests.',
    branch: 'agent/activity-pagination',
    attempt: 1,
  },
  {
    id: 'BP-103',
    title: 'Recover tasks after a lost worker heartbeat',
    type: 'Reliability',
    status: 'COMPLETED',
    description:
      'Detect a missing heartbeat, release the execution slot, and make the interrupted task eligible for a bounded retry.',
    branch: 'agent/heartbeat-recovery',
    attempt: 1,
  },
  {
    id: 'BP-102',
    title: 'Scope artifact access to project membership',
    type: 'Security',
    status: 'COMPLETED',
    description:
      'Check project membership before returning an artifact. Test both cross-project access and expired credentials.',
    branch: 'agent/artifact-access',
    attempt: 1,
  },
  {
    id: 'BP-105',
    title: 'Document the runner enrollment workflow',
    type: 'Documentation',
    status: 'QUEUED',
    description:
      'Walk through runner registration, capabilities, and the first successful heartbeat with a reproducible example.',
    branch: 'agent/runner-guide',
    attempt: 1,
  },
];

export const statusLabels: Record<TaskStatus, string> = {
  QUEUED: 'Queued',
  PROVISIONING: 'Preparing',
  IN_PROGRESS: 'Running',
  EVALUATING: 'Needs review',
  BLOCKED: 'Blocked',
  PARKED: 'Parked',
  COMPLETED: 'Completed',
  FAILED: 'Interrupted',
  CANCELLED: 'Cancelled',
};

export type DemoAction = 'advance' | 'fail' | 'retry' | 'approve';

// Deliberately bounded simulator. The backend remains the production authority.
export function transition(task: DemoTask, action: DemoAction): DemoTask {
  if (action === 'retry' && task.status === 'FAILED')
    return { ...task, status: 'QUEUED', attempt: task.attempt + 1 };
  if (action === 'fail' && task.status === 'IN_PROGRESS')
    return { ...task, status: 'FAILED' };
  if (action === 'approve' && task.status === 'EVALUATING')
    return { ...task, status: 'COMPLETED' };
  if (action !== 'advance') return task;
  const next: Partial<Record<TaskStatus, TaskStatus>> = {
    QUEUED: 'PROVISIONING',
    PROVISIONING: 'IN_PROGRESS',
    IN_PROGRESS: 'EVALUATING',
  };
  const status = next[task.status];
  return status ? { ...task, status } : task;
}

export function phaseIndex(status: TaskStatus): number {
  return (
    (
      {
        QUEUED: 0,
        PROVISIONING: 1,
        IN_PROGRESS: 2,
        FAILED: 2,
        EVALUATING: 3,
        COMPLETED: 4,
      } as Partial<Record<TaskStatus, number>>
    )[status] ?? 0
  );
}

export function taskLog(task: DemoTask): string[] {
  const phase = phaseIndex(task.status);
  const rows = [`[queue] ${task.id} accepted · attempt ${task.attempt}`];
  if (phase >= 1)
    rows.push(
      '[scheduler] Dependency and capacity checks passed',
      '[runner] Isolated workspace allocated',
    );
  if (phase >= 2)
    rows.push(
      `[git] Checked out ${task.branch}`,
      '[agent] Inspecting implementation and test boundaries',
      '[agent] Preparing a focused change',
    );
  if (task.status === 'FAILED')
    rows.push(
      '[heartbeat] Worker disconnected',
      '[recovery] Execution stopped · retry available',
    );
  if (phase >= 3)
    rows.push(
      '[checks] Sample test suite passed',
      '[artifact] Patch and review summary prepared',
      '[review] Waiting for operator approval',
    );
  if (phase >= 4)
    rows.push(
      '[review] Approved · task completed',
      '[release] Deployment is a separate verification gate',
    );
  return rows;
}
