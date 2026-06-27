import type { Task, TaskStatus } from '../types/task';

const RUNNING_CANCEL_STATUSES = new Set<TaskStatus>(['IN_PROGRESS', 'PROVISIONING']);
const WAITING_CANCEL_STATUSES = new Set<TaskStatus>(['QUEUED', 'BLOCKED', 'PARKED', 'EVALUATING']);

export const CANCELLABLE_TASK_STATUSES = new Set<TaskStatus>([
  ...RUNNING_CANCEL_STATUSES,
  ...WAITING_CANCEL_STATUSES,
]);

export function isCancellableTaskStatus(status: TaskStatus): boolean {
  return CANCELLABLE_TASK_STATUSES.has(status);
}

function hasRecordedVm(task: Task): boolean {
  return typeof task.vm_instance_id === 'string' && task.vm_instance_id.trim().length > 0;
}

function recordedVmSuffix(task: Task): string {
  return hasRecordedVm(task)
    ? ' A VM is recorded for this task; cancellation will request cleanup if it is still attached.'
    : '';
}

export function taskCancelConsequence(task: Task): string {
  switch (task.status) {
    case 'IN_PROGRESS':
    case 'PROVISIONING':
      return 'Stops the agent and cleans up its VM. In-progress work is discarded and not saved.';
    case 'QUEUED':
      return `Cancels before it starts and prevents an agent from being launched.${recordedVmSuffix(task)}`;
    case 'BLOCKED':
      return `Cancels this blocked task and prevents it from resuming when blockers clear.${recordedVmSuffix(task)}`;
    case 'PARKED':
      return `Cancels this parked task and prevents future wake or resume work.${recordedVmSuffix(task)}`;
    case 'EVALUATING':
      return `Cancels evaluation and halts further progress on this task.${recordedVmSuffix(task)}`;
    default:
      return `Cancels this task and halts further progress.${recordedVmSuffix(task)}`;
  }
}

export function buildCancelTitle(task: Task, visibleBlockingCount = 0): string {
  const visibleHint = visibleBlockingCount > 0
    ? ` — ${visibleBlockingCount} visible task${visibleBlockingCount > 1 ? 's' : ''} waiting on this`
    : '';
  const consequence = taskCancelConsequence(task);
  const titleFragment = consequence.charAt(0).toLowerCase() + consequence.slice(1);
  return `Cancel — ${titleFragment}${visibleHint}`;
}

export function buildBulkCancelConsequence(tasks: Task[]): string {
  const runningCount = tasks.filter(t => RUNNING_CANCEL_STATUSES.has(t.status)).length;
  const waitingCount = tasks.filter(t => WAITING_CANCEL_STATUSES.has(t.status)).length;
  const recordedVmWaitingCount = tasks.filter(t => WAITING_CANCEL_STATUSES.has(t.status) && hasRecordedVm(t)).length;
  const total = tasks.length;

  if (total === 0) return 'No cancellable tasks selected.';

  if (runningCount === total) {
    return 'Stops the selected running/provisioning tasks, stops their agents, and cleans up their VMs. In-progress work is discarded.';
  }

  if (waitingCount === total) {
    const vmSuffix = recordedVmWaitingCount > 0
      ? ` ${recordedVmWaitingCount} selected task${recordedVmWaitingCount > 1 ? 's have' : ' has'} a recorded VM; cancellation will request cleanup if still attached.`
      : ' No VM termination is assumed for these statuses.';
    return `Cancels the selected queued, blocked, parked, or evaluating tasks and prevents or halts further progress.${vmSuffix}`;
  }

  const waitingSuffix = waitingCount > 0
    ? ` ${waitingCount} queued, blocked, parked, or evaluating task${waitingCount > 1 ? 's' : ''} will be prevented from making further progress.`
    : '';
  const vmSuffix = recordedVmWaitingCount > 0
    ? ` ${recordedVmWaitingCount} non-running task${recordedVmWaitingCount > 1 ? 's have' : ' has'} a recorded VM; cancellation will request cleanup if still attached.`
    : '';

  return `Cancels ${total} selected task${total > 1 ? 's' : ''}. ${runningCount} running/provisioning task${runningCount > 1 ? 's' : ''} will have agents stopped and VMs cleaned up.${waitingSuffix}${vmSuffix}`;
}
