import type { KanbanColumn } from './components/KanbanBoard';

export const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED'] },
  { id: 'PROVISIONING', label: 'Starting', includes: ['PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In progress', includes: ['IN_PROGRESS'] },
  { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
  { id: 'BLOCKED', label: 'Blocked', includes: ['BLOCKED'] },
  { id: 'PARKED', label: 'Parked', includes: ['PARKED'] },
  { id: 'FAILED', label: 'Failed', includes: ['FAILED'] },
  { id: 'CANCELLED', label: 'Cancelled', includes: ['CANCELLED'] },
  { id: 'COMPLETED', label: 'Completed', includes: ['COMPLETED'] },
];

export const SORT_KEY = 'blueprint-admin:kanban-sort';
export const CANCELLABLE_STATUSES = new Set(['QUEUED', 'IN_PROGRESS', 'PROVISIONING', 'EVALUATING', 'BLOCKED', 'PARKED']);
export const TERMINAL_STATUSES = new Set(['COMPLETED', 'CANCELLED', 'FAILED']);
export const ACTIVE_PIPELINE_STATUSES = new Set(['IN_PROGRESS', 'PROVISIONING', 'EVALUATING']);
// Reservations and rollups can be live in the pipeline without executing any
// work. Operator-facing "running" counts therefore require an actually claimed
// task, not merely PROVISIONING or EVALUATING lifecycle state.
export const EXECUTING_PIPELINE_STATUSES = new Set(['IN_PROGRESS']);

export function isLocalPipelineAttentionTask(status: string, isStuck: boolean): boolean {
  return status === 'FAILED' || status === 'BLOCKED' || isStuck;
}
