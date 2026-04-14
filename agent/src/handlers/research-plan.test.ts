import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  calcMessageAge,
  parseResearchContext,
  aggregateDirectionStats,
  mapSubtaskToSpawnPayload,
} from './research-plan.js';

describe('calcMessageAge', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2024-01-01T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns ? when created_at is missing', () => {
    expect(calcMessageAge({})).toBe('?');
  });

  it('returns hours for < 24h (2h ago)', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3600000).toISOString();
    expect(calcMessageAge({ created_at: twoHoursAgo })).toBe('2h');
  });

  it('returns 23h for 23 hours ago', () => {
    const twentyThreeHoursAgo = new Date(Date.now() - 23 * 3600000).toISOString();
    expect(calcMessageAge({ created_at: twentyThreeHoursAgo })).toBe('23h');
  });

  it('returns 1d for 25 hours ago', () => {
    const twentyFiveHoursAgo = new Date(Date.now() - 25 * 3600000).toISOString();
    expect(calcMessageAge({ created_at: twentyFiveHoursAgo })).toBe('1d');
  });

  it('returns 2d for 48 hours ago', () => {
    const fortyEightHoursAgo = new Date(Date.now() - 48 * 3600000).toISOString();
    expect(calcMessageAge({ created_at: fortyEightHoursAgo })).toBe('2d');
  });

  it('returns ? for invalid date string', () => {
    expect(calcMessageAge({ created_at: 'not-a-date' })).toBe('?');
  });
});

describe('parseResearchContext', () => {
  it('returns empty arrays for empty string', () => {
    expect(parseResearchContext('')).toEqual({ established: [], failed: [], open: [], eviction: '' });
  });

  it('parses only established items', () => {
    const ctx = '## Established Findings\n- item 1\n- item 2\n';
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual(['- item 1', '- item 2']);
    expect(result.failed).toEqual([]);
    expect(result.open).toEqual([]);
  });

  it('parses only failed items', () => {
    const ctx = '## Failed Approaches\n- failed 1\n';
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual([]);
    expect(result.failed).toEqual(['- failed 1']);
    expect(result.open).toEqual([]);
  });

  it('parses mixed sections', () => {
    const ctx = `## Established Findings
- finding 1
## Failed Approaches
- failed 1
## Open Questions
- question 1`;
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual(['- finding 1']);
    expect(result.failed).toEqual(['- failed 1']);
    expect(result.open).toEqual(['- question 1']);
  });

  it('ignores lines before any section header', () => {
    const ctx = 'This is a line without a section header\n- bullet without section';
    expect(parseResearchContext(ctx)).toEqual({ established: [], failed: [], open: [], eviction: '' });
  });

  it('ignores non-bullet lines within sections', () => {
    const ctx = `## Established Findings
not a bullet
- real bullet`;
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual(['- real bullet']);
  });

  it('returns empty eviction when no eviction summary section', () => {
    const ctx = '## Established Findings\n- item 1\n';
    const result = parseResearchContext(ctx);
    expect(result.eviction).toBe('');
  });

  it('parses eviction summary into eviction field', () => {
    const ctx = `## Established Findings
- item 1
## Eviction Summary
5 established, 2 failed evicted`;
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual(['- item 1']);
    expect(result.eviction).toBe('5 established, 2 failed evicted');
  });

  it('parses eviction summary alongside all other sections', () => {
    const ctx = `## Established Findings
- finding 1
## Failed Approaches
- failed 1
## Open Questions
- question 1
## Eviction Summary
3 established evicted`;
    const result = parseResearchContext(ctx);
    expect(result.established).toEqual(['- finding 1']);
    expect(result.failed).toEqual(['- failed 1']);
    expect(result.open).toEqual(['- question 1']);
    expect(result.eviction).toBe('3 established evicted');
  });
});

describe('aggregateDirectionStats', () => {
  it('returns empty string for empty history and empty aggregate', () => {
    expect(aggregateDirectionStats([], {})).toBe('');
  });

  it('returns stats string for single cycle with 1 task', () => {
    const history = [{ tasks_created: 1, prs_merged: 0, tasks_failed: 0 }];
    expect(aggregateDirectionStats(history, {})).toBe('Stats: 1 cycles, 1 tasks');
  });

  it('includes merged count when merges exist', () => {
    const history = [{ tasks_created: 2, prs_merged: 1, tasks_failed: 0 }];
    const result = aggregateDirectionStats(history, {});
    expect(result).toContain('1 merged');
  });

  it('includes failed count when failures exist', () => {
    const history = [{ tasks_created: 2, prs_merged: 0, tasks_failed: 1 }];
    const result = aggregateDirectionStats(history, {});
    expect(result).toContain('1 failed');
  });

  it('aggregates history entries and aggregate object together', () => {
    const history = [{ tasks_created: 1, prs_merged: 0, tasks_failed: 0 }];
    const aggregate = { total_cycles: 5, total_tasks_created: 10, total_prs_merged: 3, total_tasks_failed: 2 };
    const result = aggregateDirectionStats(history, aggregate);
    expect(result).toBe('Stats: 6 cycles, 11 tasks, 3 merged, 2 failed');
  });

  it('returns empty string when totalCycles is 0 from aggregate', () => {
    expect(aggregateDirectionStats([], { total_cycles: 0 })).toBe('');
  });
});

describe('mapSubtaskToSpawnPayload', () => {
  it('maps minimal subtask with default type, scope, risk', () => {
    const result = mapSubtaskToSpawnPayload({ title: 'Test task', prompt: 'Do something' }, 'main');
    expect(result.type).toBe('create');
    expect(result.scope).toBe('small');
    expect(result.risk).toBe('low');
    expect(result.base_branch).toBe('main');
    expect(result.title).toBe('Test task');
    expect(result.prompt).toBe('Do something');
  });

  it('maps full subtask with all provided fields', () => {
    const st = {
      title: 'Full task',
      prompt: 'Full prompt',
      type: 'refactor',
      scope: 'large',
      risk: 'high',
      model: 'claude-opus-4-6',
      effort: 'high',
      approval_reason: 'dangerous change',
      depends_on_indices: [0, 1],
      related_files: ['src/main.py'],
    };
    const result = mapSubtaskToSpawnPayload(st, 'develop');
    expect(result.type).toBe('refactor');
    expect(result.scope).toBe('large');
    expect(result.risk).toBe('high');
    expect(result.model).toBe('claude-opus-4-6');
    expect(result.effort).toBe('high');
    expect(result.approval_reason).toBe('dangerous change');
    expect(result.base_branch).toBe('develop');
    expect(result.depends_on_indices).toEqual([0, 1]);
    expect(result.related_files).toEqual(['src/main.py']);
  });

  it('omits model and effort keys when not present in subtask', () => {
    const st = { title: 'Task', prompt: 'Do something' };
    const result = mapSubtaskToSpawnPayload(st, 'main');
    expect('model' in result).toBe(false);
    expect('effort' in result).toBe(false);
  });

  it('includes model and effort keys when present in subtask', () => {
    const st = { title: 'Task', prompt: 'Do something', model: 'claude-opus-4-6', effort: 'high' };
    const result = mapSubtaskToSpawnPayload(st, 'main');
    expect(result.model).toBe('claude-opus-4-6');
    expect(result.effort).toBe('high');
  });

  it('uses Untitled subtask as default title when title missing', () => {
    const result = mapSubtaskToSpawnPayload({ prompt: 'Some prompt' }, 'main');
    expect(result.title).toBe('Untitled subtask');
  });
});
