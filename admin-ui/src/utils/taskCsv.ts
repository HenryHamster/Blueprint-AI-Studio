import { formatDurationMs, parseTimestamp } from './time';
import type { Task } from '../types/task';

export type TaskSortBy = 'newest' | 'oldest' | 'type' | 'title' | 'duration' | 'attempts';
export interface KanbanColumnLike {
  id: string;
  includes: string[];
}

const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const KANBAN_COLUMN_ORDER_KEY = 'kanban_card_order';
const CSV_FIELDS = [
  'task_id',
  'title',
  'type',
  'status',
  'attempt_count',
  'created_at',
  'started_at',
  'completed_at',
  'duration',
  'failure_class',
  'pr_url',
  'estimated_cost_usd',
] as const;

function getTaskType(task: Task): string {
  return task.task_type ?? ((task.payloads?.[0] as Record<string, unknown> | undefined)?.type as string | undefined) ?? 'create';
}

function formatIsoTimestamp(value: string | undefined | null): string {
  if (!value) return '';
  const ts = parseTimestamp(value).getTime();
  return Number.isNaN(ts) ? '' : new Date(ts).toISOString();
}

function getTaskEndTime(task: Task, nowMs: number): number | null {
  if (TERMINAL_STATUSES.has(task.status)) {
    const last = task.history?.[task.history.length - 1];
    if (last?.timestamp) {
      const ts = parseTimestamp(last.timestamp).getTime();
      if (!Number.isNaN(ts)) return ts;
    }
    const createdTs = parseTimestamp(task.created_at).getTime();
    return Number.isNaN(createdTs) ? null : createdTs;
  }

  const createdTs = parseTimestamp(task.created_at).getTime();
  if (Number.isNaN(createdTs)) return null;
  return nowMs;
}

function formatTaskDuration(task: Task, nowMs: number): string {
  const createdTs = parseTimestamp(task.created_at).getTime();
  if (Number.isNaN(createdTs)) return '';
  const endTs = getTaskEndTime(task, nowMs);
  if (endTs === null) return '';
  return formatDurationMs(Math.max(0, endTs - createdTs));
}

function formatTaskCompletedAt(task: Task): string {
  if (!TERMINAL_STATUSES.has(task.status)) return '';
  const last = task.history?.[task.history.length - 1];
  if (last?.timestamp) {
    const ts = parseTimestamp(last.timestamp).getTime();
    if (!Number.isNaN(ts)) return new Date(ts).toISOString();
  }
  return formatIsoTimestamp(task.created_at);
}

function sortValue(task: Task, sortBy: TaskSortBy, nowMs: number): number | string {
  switch (sortBy) {
    case 'newest':
      return parseTimestamp(task.created_at).getTime();
    case 'oldest':
      return parseTimestamp(task.created_at).getTime();
    case 'type':
      return getTaskType(task);
    case 'title':
      return task.title ?? '';
    case 'duration': {
      const createdTs = parseTimestamp(task.created_at).getTime();
      if (Number.isNaN(createdTs)) return -1;
      const endTs = getTaskEndTime(task, nowMs);
      if (endTs === null) return -1;
      return Math.max(0, endTs - createdTs);
    }
    case 'attempts':
      return task.attempt_count ?? 0;
  }
}

export function sortTasks(tasks: Task[], sortBy: TaskSortBy, nowMs = Date.now()): Task[] {
  return [...tasks].sort((a, b) => {
    switch (sortBy) {
      case 'newest':
        return (sortValue(b, sortBy, nowMs) as number) - (sortValue(a, sortBy, nowMs) as number);
      case 'oldest':
        return (sortValue(a, sortBy, nowMs) as number) - (sortValue(b, sortBy, nowMs) as number);
      case 'duration':
      case 'attempts':
        return (sortValue(b, sortBy, nowMs) as number) - (sortValue(a, sortBy, nowMs) as number);
      case 'type':
      case 'title':
        return String(sortValue(a, sortBy, nowMs)).localeCompare(String(sortValue(b, sortBy, nowMs)));
    }
  });
}

function loadSavedKanbanColumnOrder(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(KANBAN_COLUMN_ORDER_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function orderTasksForKanbanExport(tasks: Task[], columns: KanbanColumnLike[], sortBy: TaskSortBy, nowMs = Date.now()): Task[] {
  const savedColumnOrder = loadSavedKanbanColumnOrder();
  return columns.flatMap((column) => {
    const columnTasks = tasks.filter((task) => column.includes.includes(task.status));
    const sortedTasks = sortTasks(columnTasks, sortBy, nowMs);
    const savedOrder = savedColumnOrder[column.id] ?? [];

    if (savedOrder.length === 0) return sortedTasks;

    const taskMap = new Map(sortedTasks.map((task) => [task.id, task]));
    const ordered: Task[] = [];
    for (const taskId of savedOrder) {
      const task = taskMap.get(taskId);
      if (task) {
        ordered.push(task);
        taskMap.delete(taskId);
      }
    }
    ordered.push(...taskMap.values());
    return ordered;
  });
}

export function escapeCsv(value: string | number | null | undefined): string {
  const text = value == null ? '' : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function downloadText(filename: string, mimeType: string, text: string) {
  const blob = new Blob([text], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function slugifyProjectName(projectName: string): string {
  return projectName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'project';
}

function formatStamp(nowMs: number): string {
  const d = new Date(nowMs);
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const min = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${yyyy}${mm}${dd}-${hh}${min}${ss}`;
}

export function buildTaskCsvFilename(projectName: string | null | undefined, nowMs = Date.now()): string {
  const slug = slugifyProjectName(projectName ?? 'project');
  return `tasks-${slug}-${formatStamp(nowMs)}.csv`;
}

export function buildTaskCsv(tasks: Task[], nowMs = Date.now()): string {
  const rows = tasks.map((task) => ([
    task.id,
    task.title ?? '',
    getTaskType(task),
    task.status,
    task.attempt_count ?? 0,
    formatIsoTimestamp(task.created_at),
    formatIsoTimestamp(task.started_at),
    formatTaskCompletedAt(task),
    formatTaskDuration(task, nowMs),
    task.failure_class ?? '',
    task.pr_url ?? '',
    task.estimated_cost_usd == null ? '' : task.estimated_cost_usd.toFixed(2),
  ]));

  return [
    CSV_FIELDS.join(','),
    ...rows.map((row) => row.map((value) => escapeCsv(value)).join(',')),
  ].join('\n');
}
