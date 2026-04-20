import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
  },
}));

import fs from 'fs';
import {
  calcMessageAge,
  parseResearchContext,
  aggregateDirectionStats,
  classifyFailureCategory,
  buildScopeCalibrationSignal,
  buildFilteredFindingsSection,
  formatCochangeHints,
  formatDecompositionOutcomes,
  readProgressArtifact,
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

  it('includes per-type success rates when scope_breakdown spans multiple types', () => {
    const history = [
      { tasks_created: 3, prs_merged: 2, scope_breakdown: { small: { created: 3, merged: 2 } } },
      { tasks_created: 2, prs_merged: 0, scope_breakdown: { medium: { created: 2, merged: 0 } } },
    ];
    const result = aggregateDirectionStats(history, {});
    expect(result).toContain('by type: small 67% (2/3), medium 0% (0/2)');
  });

  it('includes dedup filter breakdown when findings were filtered', () => {
    const history = [
      { tasks_created: 2, prs_merged: 1, filter_breakdown: { hash_dedup: 2, word_overlap_dedup: 1, file_overlap_dedup: 0, type_filtered: 0, empty_prompt: 0 } },
      { tasks_created: 1, prs_merged: 0, filter_breakdown: { hash_dedup: 1, word_overlap_dedup: 0, file_overlap_dedup: 3, type_filtered: 0, empty_prompt: 0 } },
    ];
    const result = aggregateDirectionStats(history, {});
    expect(result).toContain('filtered: 7 (hash 3, word-overlap 1, file-overlap 3)');
  });

  it('omits filter breakdown when no findings were filtered', () => {
    const history = [{ tasks_created: 2, prs_merged: 1, filter_breakdown: { hash_dedup: 0, word_overlap_dedup: 0 } }];
    const result = aggregateDirectionStats(history, {});
    expect(result).not.toContain('filtered:');
  });

  it('omits per-type breakdown when only one type present', () => {
    const history = [{ tasks_created: 3, prs_merged: 2, scope_breakdown: { small: { created: 3, merged: 2 } } }];
    const result = aggregateDirectionStats(history, {});
    expect(result).not.toContain('by type:');
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

  it('surfaces prior-cycle CODE_ERRORs with titles and reasons when history has earlier failures', () => {
    const history = [
      { failed_task_details: [{ title: 'feat: refactor module A boundary cleanup', reason: 'review iterations exhausted', category: 'claude_code_failed' }] },
      { failed_task_details: [{ title: 'fix: recent', reason: 'build failed', category: 'build_failed' }] },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Prior CODE_ERRORs to avoid repeating');
    expect(result).toContain('refactor module A boundary cleanup');
    expect(result).toContain('review iterations exhausted');
    expect(result).toContain('1 cycle ago');
  });

  it('does not emit prior-failures line when only one cycle exists', () => {
    const history = [
      { failed_task_details: [{ title: 'fix: only', reason: 'build failed', category: 'build_failed' }] },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).not.toContain('Prior CODE_ERRORs');
  });

  it('surfaces per-scope expansion ratio when scope_breakdown carries avg_expansion_ratio', () => {
    const history = [
      {
        failed_task_details: [],
        scope_breakdown: {
          small: { created: 3, merged: 2, failed: 0, avg_expansion_ratio: 1.2 },
        },
      },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Scope accuracy');
    expect(result).toContain('small 1.2x');
    expect(result).not.toContain('scope overflow');
  });

  it('warns when expansion ratio exceeds 2.0x', () => {
    const history = [
      {
        failed_task_details: [],
        scope_breakdown: {
          medium: { created: 2, merged: 1, failed: 1, avg_expansion_ratio: 3.0 },
        },
      },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('medium 3.0x');
    expect(result).toContain('scope overflow');
    expect(result).toContain('Expansion ratio exceeds 2.0x');
  });

  it('surfaces per-scope profile with merged complexity/expansion and failed complexity', () => {
    const history = [{
      scope_breakdown: {
        small: { created: 3, merged: 2, failed: 1, avg_complexity_merged: 0.72, avg_complexity_failed: 0.41, avg_expansion_ratio: 1.3 },
        medium: { created: 2, merged: 0, failed: 2, avg_complexity_failed: 0.30 },
      },
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Per-scope profile');
    expect(result).toContain('small(merged cplx 0.72/expand 1.3x vs failed cplx 0.41)');
    expect(result).toContain('medium(failed cplx 0.30)');
  });

  it('surfaces calibration counts for calibrated_in and calibrated_out', () => {
    const history = [{
      scope_breakdown: {
        small: { created: 2, merged: 2, failed: 0, calibrated_in: 2 },
        medium: { created: 0, merged: 0, failed: 0, calibrated_out: 2 },
      },
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Calibration');
    expect(result).toContain('small in=2');
    expect(result).toContain('medium out=2');
  });

  it('omits profile and calibration lines when those fields are absent', () => {
    const history = [{
      scope_breakdown: { small: { created: 2, merged: 1, failed: 1 } },
    }];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('Scope success');
    expect(result).not.toContain('Per-scope profile');
    expect(result).not.toContain('Calibration');
  });

  it('averages per-scope avg_complexity_merged across multiple cycles', () => {
    const history = [
      { scope_breakdown: { small: { created: 1, merged: 1, avg_complexity_merged: 0.60 } } },
      { scope_breakdown: { small: { created: 1, merged: 1, avg_complexity_merged: 0.80 } } },
    ];
    const result = buildScopeCalibrationSignal(history);
    expect(result).toContain('small(merged cplx 0.70)');
  });
});

describe('buildFilteredFindingsSection', () => {
  it('returns empty string for empty history', () => {
    expect(buildFilteredFindingsSection([])).toBe('');
  });

  it('returns empty string when no entries carry filtered_findings', () => {
    const history = [
      { finding_titles: ['a', 'b'], tasks_created: 2 },
      { finding_titles: ['c'], tasks_created: 1 },
    ];
    expect(buildFilteredFindingsSection(history)).toBe('');
  });

  it('renders title and reason lines from the most recent cycle', () => {
    const history = [
      {
        filtered_findings: [
          { title: 'Consolidate token refresh', reason: 'hash match with prior task' },
          { title: 'Add pipeline metrics', reason: 'word overlap with: instrument pipeline metrics' },
        ],
      },
    ];
    const section = buildFilteredFindingsSection(history);
    expect(section).toContain('Recently filtered (avoid');
    expect(section).toContain('"Consolidate token refresh"');
    expect(section).toContain('hash match with prior task');
    expect(section).toContain('"Add pipeline metrics"');
    expect(section).toContain('word overlap with: instrument pipeline metrics');
  });

  it('dedups titles across cycles and keeps the most recent reason', () => {
    const history = [
      { filtered_findings: [{ title: 'Shared title', reason: 'older reason' }] },
      { filtered_findings: [{ title: 'Shared title', reason: 'newer reason' }] },
    ];
    const section = buildFilteredFindingsSection(history);
    // Only one occurrence of the title, with the newer reason attached.
    const titleMatches = section.match(/Shared title/g) || [];
    expect(titleMatches.length).toBe(1);
    expect(section).toContain('newer reason');
    expect(section).not.toContain('older reason');
  });

  it('caps rendered entries at 5', () => {
    const history = [
      {
        filtered_findings: Array.from({ length: 10 }, (_, i) => ({
          title: `finding ${i}`,
          reason: 'hash match with prior task',
        })),
      },
    ];
    const section = buildFilteredFindingsSection(history);
    const bullets = section.split('\n').filter(l => l.trim().startsWith('*'));
    expect(bullets.length).toBe(5);
  });

  it('only considers the last 3 history entries', () => {
    const history = [
      { filtered_findings: [{ title: 'ancient', reason: 'ancient reason' }] },
      { filtered_findings: [{ title: 'older', reason: 'older reason' }] },
      { filtered_findings: [{ title: 'cycle -2', reason: 'r1' }] },
      { filtered_findings: [{ title: 'cycle -1', reason: 'r2' }] },
      { filtered_findings: [{ title: 'cycle 0', reason: 'r3' }] },
    ];
    const section = buildFilteredFindingsSection(history);
    expect(section).toContain('cycle 0');
    expect(section).toContain('cycle -1');
    expect(section).toContain('cycle -2');
    expect(section).not.toContain('ancient');
    expect(section).not.toContain('older');
  });

  it('renders title without trailing separator when reason is empty', () => {
    const history = [
      { filtered_findings: [{ title: 'just a title', reason: '' }] },
    ];
    const section = buildFilteredFindingsSection(history);
    expect(section).toContain('"just a title"');
    expect(section).not.toContain('just a title" —');
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

describe('readProgressArtifact', () => {
  beforeEach(() => {
    vi.mocked(fs.existsSync).mockReset().mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReset().mockReturnValue('');
  });

  it('returns prefixed content when file exists with content', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('  progress notes here  ');
    const result = readProgressArtifact('task-123', 'plan');
    expect(result).toBe('## Pre-Implementation Analysis\n\nprogress notes here');
    expect(vi.mocked(fs.existsSync)).toHaveBeenCalledWith('/tmp/plan_progress_task-123.md');
    expect(vi.mocked(fs.readFileSync)).toHaveBeenCalledWith('/tmp/plan_progress_task-123.md', 'utf-8');
  });

  it('returns undefined when file does not exist', () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    expect(readProgressArtifact('task-456', 'research')).toBeUndefined();
    expect(vi.mocked(fs.readFileSync)).not.toHaveBeenCalled();
  });

  it('returns undefined when file exists but is whitespace-only', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('   \n\t  \n');
    expect(readProgressArtifact('task-789', 'research')).toBeUndefined();
  });

  it('returns undefined when fs.readFileSync throws', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockImplementation(() => {
      throw new Error('permission denied');
    });
    expect(readProgressArtifact('task-err', 'plan')).toBeUndefined();
  });
});
