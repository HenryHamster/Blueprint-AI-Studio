import type { KanbanColumn } from './components/KanbanBoard';

export const COLUMNS: KanbanColumn[] = [
  { id: 'QUEUED', label: 'Queued', includes: ['QUEUED', 'PROVISIONING'] },
  { id: 'IN_PROGRESS', label: 'In Progress', includes: ['IN_PROGRESS'] },
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
