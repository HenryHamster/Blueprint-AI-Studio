import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  calcMessageAge,
  parseResearchContext,
  aggregateDirectionStats,
  mapSubtaskToSpawnPayload,
  classifyFailureCategory,
  buildScopeCalibrationSignal,
  formatCochangeHints,
  formatDecompositionOutcomes,
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
    expect(aggregateDirectionStats(history, {})).toBe('Stats: 1 cycles, 1 tasks, conversion: 0% (0/1 findings merged last 1 cycle)');
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
    expect(result).toBe('Stats: 6 cycles, 11 tasks, 3 merged, 2 failed, conversion: 0% (0/1 findings merged last 1 cycle)');
  });

  it('includes conversion rate from last 3 cycles when tasks were created', () => {
    const history = [
      { tasks_created: 4, prs_merged: 1, tasks_failed: 0 },
      { tasks_created: 4, prs_merged: 1, tasks_failed: 0 },
    ];
    const result = aggregateDirectionStats(history, {});
    expect(result).toContain('conversion: 25% (2/8 findings merged last 2 cycles)');
  });

  it('omits conversion rate when no tasks were created in recent history', () => {
    const history = [{ tasks_created: 0, prs_merged: 0, tasks_failed: 0 }];
    const result = aggregateDirectionStats(history, {});
    expect(result).not.toContain('conversion:');
  });

  it('uses at most last 3 cycles for conversion rate', () => {
    const history = [
      { tasks_created: 10, prs_merged: 10, tasks_failed: 0 },  // older, ignored
      { tasks_created: 4, prs_merged: 0, tasks_failed: 0 },
      { tasks_created: 4, prs_merged: 0, tasks_failed: 0 },
      { tasks_created: 4, prs_merged: 0, tasks_failed: 0 },
    ];
    const result = aggregateDirectionStats(history, {});
    // last 3 cycles: 12 tasks, 0 merged → 0%
    expect(result).toContain('conversion: 0% (0/12 findings merged last 3 cycles)');
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

describe('classifyFailureCategory', () => {
  it('classifies build/test/lint/parse/fatal/claude_code_failed as CODE_ERROR', () => {
    for (const cat of ['build_failed', 'test_failed', 'lint_failed', 'parse_failed', 'fatal', 'claude_code_failed']) {
      expect(classifyFailureCategory(cat)).toBe('CODE_ERROR');
    }
  });

  it('classifies clone_failed as PROVISION_FAILURE', () => {
    expect(classifyFailureCategory('clone_failed')).toBe('PROVISION_FAILURE');
  });

  it('classifies rate_limited as RATE_LIMIT', () => {
    expect(classifyFailureCategory('rate_limited')).toBe('RATE_LIMIT');
  });

  it('classifies pr_creation_failed as EXTERNAL_API', () => {
    expect(classifyFailureCategory('pr_creation_failed')).toBe('EXTERNAL_API');
  });

  it('passes through unknown categories unchanged', () => {
    expect(classifyFailureCategory('some_unknown_cat')).toBe('some_unknown_cat');
  });

  it('returns unknown for empty string', () => {
    expect(classifyFailureCategory('')).toBe('unknown');
  });
});

describe('buildScopeCalibrationSignal', () => {
  it('returns empty string for empty history', () => {
    expect(buildScopeCalibrationSignal([])).toBe('');
  });

  it('returns empty string when last entry has no failed_task_details', () => {
    expect(buildScopeCalibrationSignal([{ tasks_created: 2 }])).toBe('');
  });

  it('returns empty string when failed_task_details is empty array', () => {
    expect(buildScopeCalibrationSignal([{ failed_task_details: [] }])).toBe('');
  });

  it('includes CODE_ERROR signal with task names when last cycle has code errors', () => {
    const history = [{
      failed_task_details: [
        { title: 'fix: auth refresh', reason: 'review iterations exhausted', category: 'claude_code_failed' },
      ],
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('CODE_ERROR');
    expect(result).toContain('"fix: auth refresh"');
    expect(result).toContain('over-scoped/under-specified');
  });

  it('includes infra signal when last cycle has clone/provision failures', () => {
    const history = [{
      failed_task_details: [
        { title: 'feat: db migration', reason: 'vm failed to start', category: 'clone_failed' },
      ],
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('infra failure');
    expect(result).toContain('retry ok');
  });

  it('shows scope trend line when CODE_ERRORs recur across multiple cycles', () => {
    const history = [
      {
        failed_task_details: [
          { title: 'feat: prev task', reason: 'build failed', category: 'build_failed' },
        ],
      },
      {
        failed_task_details: [
          { title: 'fix: auth refresh', reason: 'claude_code_failed', category: 'claude_code_failed' },
        ],
      },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Scope trend');
    expect(result).toContain('smaller');
  });

  it('does not show scope trend when only last cycle has CODE_ERRORs', () => {
    const history = [
      { failed_task_details: [] },
      {
        failed_task_details: [
          { title: 'fix: auth refresh', reason: 'build failed', category: 'build_failed' },
        ],
      },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('CODE_ERROR');
    expect(result).not.toContain('Scope trend');
  });

  it('handles mixed failure types and groups them correctly', () => {
    const history = [{
      failed_task_details: [
        { title: 'feat: A', reason: '', category: 'build_failed' },
        { title: 'feat: B', reason: '', category: 'clone_failed' },
      ],
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('1 CODE_ERROR');
    expect(result).toContain('1 infra failure');
  });

  it('shows scope success line from scope_breakdown across last 3 cycles', () => {
    const history = [
      { scope_breakdown: { small: { created: 3, merged: 2, failed: 1 }, medium: { created: 2, merged: 1, failed: 1 } } },
      { scope_breakdown: { small: { created: 2, merged: 2, failed: 0 } } },
      { scope_breakdown: { large: { created: 1, merged: 0, failed: 1 } } },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Scope success (last 3 cycles)');
    expect(result).toContain('small 4/5');
    expect(result).toContain('medium 1/2');
    expect(result).toContain('large 0/1');
  });

  it('shows scope success even when there are no failures in last cycle', () => {
    const history = [{
      failed_task_details: [],
      scope_breakdown: { small: { created: 4, merged: 3, failed: 1 } },
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Scope success (last 1 cycle)');
    expect(result).toContain('small 3/4');
    expect(result).not.toContain('Last cycle failures');
  });

  it('aggregates scope_breakdown from up to 3 most recent cycles only', () => {
    const history = [
      { scope_breakdown: { small: { created: 10, merged: 10, failed: 0 } } }, // older, excluded
      { scope_breakdown: { small: { created: 1, merged: 1, failed: 0 } } },
      { scope_breakdown: { small: { created: 1, merged: 0, failed: 1 } } },
      { scope_breakdown: { small: { created: 1, merged: 1, failed: 0 } } },
    ];
    const result = buildScopeCalibrationSignal(history);
    // Only last 3 cycles: 1+1+1=3 created, 1+0+1=2 merged
    expect(result).toContain('small 2/3');
    expect(result).not.toContain('small 12/13');
  });

  it('omits scope success line when scope_breakdown is absent from all recent cycles', () => {
    const history = [
      { failed_task_details: [{ title: 'feat: X', reason: 'build failed', category: 'build_failed' }] },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('CODE_ERROR');
    expect(result).not.toContain('Scope success');
  });
});

describe('formatCochangeHints', () => {
  it('returns empty string when direction context is undefined', () => {
    expect(formatCochangeHints(undefined)).toBe('');
  });

  it('returns empty string when cochange_hints is missing', () => {
    expect(formatCochangeHints({ other: 'data' })).toBe('');
  });

  it('returns empty string when cochange_hints is empty', () => {
    expect(formatCochangeHints({ cochange_hints: {} })).toBe('');
  });

  it('renders deduplicated pairs sorted by score (highest first)', () => {
    const ctx = {
      cochange_hints: {
        'a.py': { 'b.py': 1.5, 'c.py': 3.0 },
        'b.py': { 'a.py': 1.5 },
        'c.py': { 'a.py': 3.0 },
      },
    };
    const result = formatCochangeHints(ctx);
    expect(result).toContain('Co-Change Affinity Hints');
    // Each pair appears once (deduplicated)
    const aBMatches = result.match(/a\.py.*b\.py/g) || [];
    const aCMatches = result.match(/a\.py.*c\.py/g) || [];
    expect(aBMatches.length).toBe(1);
    expect(aCMatches.length).toBe(1);
    // Higher-affinity pair comes first
    expect(result.indexOf('c.py')).toBeLessThan(result.indexOf('b.py'));
    expect(result).toContain('affinity: 3');
    expect(result).toContain('affinity: 1.5');
  });

  it('skips non-numeric scores gracefully', () => {
    const ctx = {
      cochange_hints: {
        'a.py': { 'b.py': 'not-a-number' as unknown as number, 'c.py': 2.0 },
      },
    };
    const result = formatCochangeHints(ctx);
    expect(result).toContain('a.py');
    expect(result).toContain('c.py');
    expect(result).not.toContain('b.py');
  });
});

describe('formatDecompositionOutcomes', () => {
  it('returns empty string when direction context is undefined', () => {
    expect(formatDecompositionOutcomes(undefined)).toBe('');
  });

  it('returns empty string when decomposition_outcomes is missing', () => {
    expect(formatDecompositionOutcomes({ other: 'x' })).toBe('');
  });

  it('returns empty string when prior_decomposition_count is zero', () => {
    expect(formatDecompositionOutcomes({
      decomposition_outcomes: { prior_decomposition_count: 0 },
    })).toBe('');
  });

  it('renders section with metrics when data is present', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 4,
        avg_subtask_merge_rate: 0.75,
        avg_files_per_subtask: 3.5,
        max_files_in_subtask: 6,
        avg_dependency_depth: 2.0,
        max_dependency_depth: 3,
        high_cross_module_correlates_with_low_merge: false,
      },
    });
    expect(result).toContain('Prior Decomposition Outcomes');
    expect(result).toContain('Prior decompositions analyzed: 4');
    expect(result).toContain('Avg subtask merge rate: 75%');
    expect(result).toContain('Avg files per subtask: 3.5');
    expect(result).toContain('Max files in a single subtask: 6');
    expect(result).toContain('Max dependency depth: 3');
    // No actionable hints when thresholds aren't tripped
    expect(result).not.toContain('Actionable guidance');
  });

  it('includes "flatten dependency graph" hint when max_dependency_depth > 3', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 2,
        avg_subtask_merge_rate: 0.5,
        max_dependency_depth: 5,
      },
    });
    expect(result).toContain('flatten the dependency graph');
  });

  it('includes "smaller subtasks" hint when avg_files_per_subtask is high and merge rate modest', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.3,
        avg_files_per_subtask: 8.0,
      },
    });
    expect(result).toContain('prefer smaller subtasks');
  });

  it('includes cross-module correlation warning when flagged', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.6,
        high_cross_module_correlates_with_low_merge: true,
      },
    });
    expect(result).toContain('Cross-module subtasks');
    expect(result).toContain('keep each subtask scoped to one module');
  });

  it('does not recommend smaller subtasks when merge rate is high despite many files', () => {
    const result = formatDecompositionOutcomes({
      decomposition_outcomes: {
        prior_decomposition_count: 3,
        avg_subtask_merge_rate: 0.9,
        avg_files_per_subtask: 8.0,
      },
    });
    expect(result).not.toContain('prefer smaller subtasks');
  });
});
