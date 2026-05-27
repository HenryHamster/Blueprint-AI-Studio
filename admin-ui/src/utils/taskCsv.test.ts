import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import type { Task } from '../types/task';
import { buildTaskCsv, buildTaskCsvFilename, escapeCsv, orderTasksForKanbanExport, sortTasks } from './taskCsv';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Task',
    description: '',
    status: 'QUEUED',
    created_at: '2026-05-27T09:00:00Z',
    created_by: 'agent',
    requires_approval: false,
    ...overrides,
  };
}

describe('taskCsv helpers', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-27T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('escapes commas, quotes, and newlines for CSV cells', () => {
    expect(escapeCsv('plain')).toBe('plain');
    expect(escapeCsv('a,b')).toBe('"a,b"');
    expect(escapeCsv('line 1\nline 2')).toBe('"line 1\nline 2"');
    expect(escapeCsv('say "yes"')).toBe('"say ""yes"""');
  });

  it('sorts tasks with the same semantics used by the kanban board', () => {
    const tasks = [
      makeTask({ id: 'newer', created_at: '2026-05-27T11:00:00Z', title: 'B', task_type: 'fix', status: 'IN_PROGRESS', started_at: '2026-05-27T11:10:00Z', attempt_count: 3 }),
      makeTask({ id: 'older', created_at: '2026-05-27T08:00:00Z', title: 'A', task_type: 'create', status: 'QUEUED', started_at: '2026-05-27T08:15:00Z', attempt_count: 1 }),
    ];

    expect(sortTasks(tasks, 'newest').map((task) => task.id)).toEqual(['newer', 'older']);
    expect(sortTasks(tasks, 'oldest').map((task) => task.id)).toEqual(['older', 'newer']);
    expect(sortTasks(tasks, 'title').map((task) => task.id)).toEqual(['older', 'newer']);
    expect(sortTasks(tasks, 'type').map((task) => task.id)).toEqual(['older', 'newer']);
    expect(sortTasks(tasks, 'duration').map((task) => task.id)).toEqual(['older', 'newer']);
    expect(sortTasks(tasks, 'attempts').map((task) => task.id)).toEqual(['newer', 'older']);
  });

  it('serializes the exported columns in order with terminal completion metadata', () => {
    const csv = buildTaskCsv([
      makeTask({
        id: 'task-2',
        title: 'Alpha, "quoted"',
        status: 'COMPLETED',
        task_type: 'fix',
        created_at: '2026-05-27T09:00:00Z',
        started_at: '2026-05-27T09:10:00Z',
        attempt_count: 2,
        failure_class: 'CODE_ERROR',
        pr_url: 'https://example.com/pr/2',
        estimated_cost_usd: 12.5,
        history: [{ timestamp: '2026-05-27T09:45:00Z', action: 'done', details: {} }],
      }),
      makeTask({
        id: 'task-1',
        title: 'Plain title',
        status: 'IN_PROGRESS',
        task_type: 'create',
        created_at: '2026-05-27T10:00:00Z',
        started_at: '2026-05-27T10:15:00Z',
      }),
    ], Date.UTC(2026, 4, 27, 12, 0, 0));

    expect(csv).toContain('task_id,title,type,status,attempt_count,created_at,started_at,completed_at,duration,failure_class,pr_url,estimated_cost_usd');
    expect(csv).toContain('task-2,"Alpha, ""quoted""",fix,COMPLETED,2,2026-05-27T09:00:00.000Z,2026-05-27T09:10:00.000Z,2026-05-27T09:45:00.000Z,45m,CODE_ERROR,https://example.com/pr/2,12.50');
    expect(csv).toContain('task-1,Plain title,create,IN_PROGRESS,0,2026-05-27T10:00:00.000Z,2026-05-27T10:15:00.000Z,,2h,,,');
  });

  it('stamps the filename with a slug and UTC timestamp', () => {
    expect(buildTaskCsvFilename('Blueprint AI Studio', Date.UTC(2026, 4, 27, 12, 34, 56))).toBe('tasks-blueprint-ai-studio-20260527-123456.csv');
  });

  it('respects the persisted kanban column order when flattening for export', () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => key === 'kanban_card_order' ? JSON.stringify({ QUEUED: ['task-2', 'task-1'] }) : null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 0,
    });

    const ordered = orderTasksForKanbanExport([
      makeTask({ id: 'task-1', title: 'A', status: 'QUEUED', created_at: '2026-05-27T09:00:00Z' }),
      makeTask({ id: 'task-2', title: 'B', status: 'QUEUED', created_at: '2026-05-27T10:00:00Z' }),
    ], [
      { id: 'QUEUED', includes: ['QUEUED'] },
    ], 'newest', Date.UTC(2026, 4, 27, 12, 0, 0));

    expect(ordered.map((task) => task.id)).toEqual(['task-2', 'task-1']);
  });
});
