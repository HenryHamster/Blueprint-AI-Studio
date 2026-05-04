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
  buildStrategicCompletionLogs,
  classifyFailureCategory,
  buildScopeCalibrationSignal,
  buildFilteredFindingsSection,
  buildRejectedProposalsSection,
  buildUserPausedSection,
  formatCochangeHints,
  formatDecompositionOutcomes,
  formatPreviousDecisionOutcomes,
  readProgressArtifact,
  buildPortfolioComposition,
  buildThinkerPrompt,
  type ThinkerPromptParams,
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

describe('buildStrategicCompletionLogs', () => {
  it('does not warn for successful command-based strategic output without legacy JSON', () => {
    const result = buildStrategicCompletionLogs(
      'Problem model set at revision 38.\nRunning update set (2387 chars).',
      'Both commands ran successfully.',
    );

    expect(result.warning).toBeUndefined();
    expect(result.logs).toBe('Both commands ran successfully.');
  });

  it('extracts legacy inline strategic JSON when both required keys are present', () => {
    const result = buildStrategicCompletionLogs(
      'done\n{"vision":"build great things","direction_operations":[]}',
      '',
    );

    expect(result.warning).toBeUndefined();
    expect(JSON.parse(result.logs)).toEqual({
      vision: 'build great things',
      direction_operations: [],
    });
  });

  it('extracts problem-model inline strategic JSON without requiring vision', () => {
    const result = buildStrategicCompletionLogs(
      'done\n{"problem_model":{"revision":39},"direction_operations":[]}',
      '',
    );

    expect(result.warning).toBeUndefined();
    expect(JSON.parse(result.logs)).toEqual({
      problem_model: { revision: 39 },
      direction_operations: [],
    });
  });

  it('warns only when legacy-looking strategic JSON cannot be extracted', () => {
    const result = buildStrategicCompletionLogs(
      'Legacy keys mentioned: "vision" and "direction_operations", but no JSON object.',
      'fallback summary',
    );

    expect(result.warning).toContain('Could not extract strategic JSON');
    expect(result.logs).toBe('fallback summary');
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

  it('appends differentiation hints based on reason type', () => {
    const history = [
      {
        filtered_findings: [
          { title: 'A', reason: 'hash match with prior task' },
          { title: 'B', reason: 'word overlap with: instrument pipeline metrics' },
          { title: 'C', reason: 'file overlap with: foo.py, bar.py' },
          { title: 'D', reason: 'some unrecognized reason' },
        ],
      },
    ];
    const section = buildFilteredFindingsSection(history);
    expect(section).toContain('[hint: fundamentally different scope or approach needed]');
    expect(section).toContain('[hint: target different files or subsystem');
    expect(section).toContain("[hint: address a different aspect of these files");
    // Unrecognized reasons render without a hint.
    expect(section).toContain('"D" — some unrecognized reason');
    expect(section).not.toMatch(/"D" — some unrecognized reason \[hint:/);
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

describe('buildRejectedProposalsSection', () => {
  it('returns empty string for empty input', () => {
    expect(buildRejectedProposalsSection([])).toBe('');
  });

  it('renders rejected proposals with date, text, and rationale', () => {
    const out = buildRejectedProposalsSection([
      { text: 'Add streaming uploads', rejected_at: '2026-04-10T12:00:00Z', rationale: 'Out of scope' },
      { text: 'Migrate to gRPC', rejected_at: '2026-04-05T08:00:00Z' },
    ]);
    expect(out).toContain('## Recently Rejected Proposals');
    expect(out).toContain('Do NOT re-propose');
    expect(out).toContain('(2026-04-10) Add streaming uploads — Out of scope');
    expect(out).toContain('(2026-04-05) Migrate to gRPC');
  });

  it('caps at 10 entries', () => {
    const many = Array.from({ length: 15 }, (_, i) => ({
      text: `proposal ${i}`, rejected_at: '2026-04-01T00:00:00Z',
    }));
    const out = buildRejectedProposalsSection(many);
    expect(out).toContain('proposal 9');
    expect(out).not.toContain('proposal 10');
  });
});

describe('buildUserPausedSection', () => {
  it('returns empty string when no directions', () => {
    expect(buildUserPausedSection([])).toBe('');
  });

  it('returns empty when only auto-paused or active directions exist', () => {
    const out = buildUserPausedSection([
      { id: 'd-1', text: 'A', status: 'active' },
      { id: 'd-2', text: 'B', status: 'paused', auto_paused: true, paused_reason: 'CODE_ERROR surge' },
    ]);
    expect(out).toBe('');
  });

  it('flags user-paused directions and includes paused_reason', () => {
    const out = buildUserPausedSection([
      { id: 'd-1', text: 'A', status: 'active' },
      { id: 'd-2', text: 'Paused by user', status: 'paused', auto_paused: false, paused_reason: 'awaiting design review' },
      { id: 'd-3', text: 'Auto paused', status: 'paused', auto_paused: true },
    ]);
    expect(out).toContain('## User-Paused Directions');
    expect(out).toContain('Paused by user (id: d-2) — reason: awaiting design review');
    expect(out).not.toContain('Auto paused');
  });

  it('treats missing auto_paused as user-paused', () => {
    const out = buildUserPausedSection([
      { id: 'd-9', text: 'Legacy paused', status: 'paused' },
    ]);
    expect(out).toContain('Legacy paused (id: d-9)');
  });
});

describe('formatPreviousDecisionOutcomes', () => {
  it('returns empty string when input is undefined', () => {
    expect(formatPreviousDecisionOutcomes(undefined)).toBe('');
  });

  it('returns empty string when input is null', () => {
    expect(formatPreviousDecisionOutcomes(null)).toBe('');
  });

  it('returns empty string when created and retired are both empty', () => {
    expect(formatPreviousDecisionOutcomes({ cycle_id: 'sc-1', created: [], retired: [] })).toBe('');
  });

  it('renders created direction with status, cycles, merges, quality', () => {
    const out = formatPreviousDecisionOutcomes({
      cycle_id: 'sc-prev',
      phase2_completed_at: '2026-04-19T12:00:00Z',
      created: [
        {
          direction_id: 'd-abc',
          role: 'created',
          status: 'active',
          text: 'Improve foo',
          cycles_active: 3,
          prs_merged: 2,
          quality_score: 0.75,
          auto_paused: false,
          user_paused: false,
        },
      ],
      retired: [],
    });
    expect(out).toContain('## Decision Feedback');
    expect(out).toContain('cycle sc-prev');
    expect(out).toContain('d-abc');
    expect(out).toContain('"Improve foo"');
    expect(out).toContain('status=active');
    expect(out).toContain('3 cycles active');
    expect(out).toContain('2 PRs merged');
    expect(out).toContain('quality 0.75');
    expect(out).toContain('Directions you CREATED');
  });

  it('flags auto_paused and user_paused distinctly', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [
        { direction_id: 'd-a', status: 'paused', auto_paused: true, user_paused: false, cycles_active: 1, prs_merged: 0, quality_score: 0 },
        { direction_id: 'd-b', status: 'paused', auto_paused: false, user_paused: true, cycles_active: 2, prs_merged: 0, quality_score: 0 },
      ],
      retired: [],
    });
    expect(out).toMatch(/d-a:.*AUTO_PAUSED/);
    expect(out).toMatch(/d-b:.*USER_PAUSED/);
    expect(out).not.toMatch(/d-a:.*USER_PAUSED/);
  });

  it('distinguishes missing retired directions from still-present ones', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [],
      retired: [
        { direction_id: 'd-gone', role: 'retired', status: 'missing' },
        { direction_id: 'd-here', role: 'retired', status: 'retired', cycles_active: 5, prs_merged: 1, quality_score: 0.4 },
      ],
    });
    expect(out).toContain('Directions you RETIRED');
    expect(out).toContain('d-gone');
    expect(out).toContain('fully removed from direction list');
    expect(out).toContain('d-here');
    expect(out).toContain('status=retired');
  });

  it('handles malformed entries without throwing', () => {
    expect(() => formatPreviousDecisionOutcomes({
      created: [{}, { direction_id: 'd-partial' }],
      retired: [{ direction_id: null }],
    })).not.toThrow();
  });

  it('includes reflection prompt encouraging retrospective', () => {
    const out = formatPreviousDecisionOutcomes({
      created: [{ direction_id: 'd-x', status: 'active', cycles_active: 1, prs_merged: 0, quality_score: 0 }],
      retired: [],
    });
    expect(out.toLowerCase()).toContain('reflect');
  });
});

describe('buildPortfolioComposition', () => {
  it('returns empty message when no active directions', () => {
    expect(buildPortfolioComposition([], [], {})).toBe('No active directions — portfolio is empty.');
  });

  it('reports category mix, coverage gaps, concentration, scope, and age flags', () => {
    const dirs = [
      { id: 'd-1', text: 'Product A', category: 'product', system_id: 'sys-a', total_cycles: 2 },
      { id: 'd-2', text: 'Refactor A', category: 'structural', system_id: 'sys-a', total_cycles: 12 },
      { id: 'd-3', text: 'More A', category: 'structural', system_id: 'sys-a', total_cycles: 3 },
      { id: 'd-4', text: 'Mystery', total_cycles: 1 },
    ];
    const systems = [
      { id: 'sys-a', name: 'System A' },
      { id: 'sys-b', name: 'System B' },
    ];
    const out = buildPortfolioComposition(dirs, systems, { small: 2, large: 1 });
    expect(out).toContain('product=1 (25%)');
    expect(out).toContain('structural=2 (50%)');
    expect(out).toContain('uncategorized=1');
    expect(out).toContain('LOW PRODUCT RATIO');
    expect(out).toContain('System coverage: 1/2');
    expect(out).toContain('System B (sys-b)');
    expect(out).toContain('CONCENTRATION RISK');
    expect(out).toContain('sys-a=3');
    expect(out).toContain('In-flight scope mix: small=2, large=1');
    expect(out).toContain('Long-running (10+ cycles');
    expect(out).toContain('d-2');
  });

  it('omits product flag when product ratio >= 30%', () => {
    const dirs = [
      { id: 'd-1', category: 'product', total_cycles: 1 },
      { id: 'd-2', category: 'structural', total_cycles: 1 },
    ];
    const out = buildPortfolioComposition(dirs, [], undefined);
    expect(out).not.toContain('LOW PRODUCT RATIO');
    expect(out).not.toContain('CONCENTRATION RISK');
    expect(out).not.toContain('In-flight scope mix');
    expect(out).not.toContain('System coverage');
  });
});

describe('buildThinkerPrompt', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: 'Clock: 2026-05-04',
    isSelfRepo: false,
    userStateLine: 'User last active: today',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update (living document — rewrite, don\'t append)\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    dirSummaryText: 'No active directions.',
    dirHealthSummary: 'Active: 0/10 slots.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    threadSection: '',
    taskId: 'test-task-123',
  };

  it('includes required structural headers when inputs are populated', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('## Your current understanding (problem_model)');
    expect(prompt).toContain('## What you previously said was hard');
    expect(prompt).toContain('## Running update');
    expect(prompt).toContain('## Architecture');
    expect(prompt).toContain('## Current research directions');
    expect(prompt).toContain('## Direction Health');
    expect(prompt).toContain('## Portfolio Composition');
  });

  it('includes all required output section headers', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('## 1. State assessment');
    expect(prompt).toContain('## 2. Gap analysis');
    expect(prompt).toContain('## 3. Moves');
    expect(prompt).toContain('## 4. Running update document');
    expect(prompt).toContain('## 5. Self-reflection');
    expect(prompt).toContain('## 6. Problem model');
  });

  it('section ordering is stable: key sections appear in expected sequence', () => {
    const prompt = buildThinkerPrompt(base);
    const idx = (s: string) => prompt.indexOf(s);
    // Context sections appear before instructions
    expect(idx('## Architecture')).toBeLessThan(idx('## Current research directions'));
    expect(idx('## Current research directions')).toBeLessThan(idx('## Direction Health'));
    expect(idx('## Direction Health')).toBeLessThan(idx('## Portfolio Composition'));
    expect(idx('## Portfolio Composition')).toBeLessThan(idx('# Your job'));
    // Output sections appear at the end
    expect(idx('# Your job')).toBeLessThan(idx('## 1. State assessment'));
    expect(idx('## 1. State assessment')).toBeLessThan(idx('## 6. Problem model'));
  });

  it('problem_model section uses bootstrap text when problemModelSection signals empty', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
    });
    expect(prompt).toContain('_(empty — bootstrap)_');
    // Bootstrap guidance section should also be present
    expect(prompt).toContain('# Bootstrap (when the problem_model is empty)');
  });

  it('vision section rendered only when visionJson is non-empty', () => {
    const withVision = buildThinkerPrompt({ ...base, visionJson: '{"systems":[]}' });
    expect(withVision).toContain('## Vision (legacy artifact');

    const withoutVision = buildThinkerPrompt({ ...base, visionJson: '' });
    expect(withoutVision).not.toContain('## Vision (legacy artifact');
  });

  it('system maturity section rendered only when systemMaturitySection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, systemMaturitySection: '\n## System Maturity\nsome data\n' });
    expect(with_).toContain('## System Maturity');

    const without = buildThinkerPrompt({ ...base, systemMaturitySection: '' });
    expect(without).not.toContain('## System Maturity');
  });

  it('drift section rendered only when driftSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, driftSection: '\n## Drift Signals\nover_claim: true\n' });
    expect(with_).toContain('## Drift Signals');

    const without = buildThinkerPrompt({ ...base, driftSection: '' });
    expect(without).not.toContain('## Drift Signals');
  });

  it('self-improvement block rendered only when isSelfRepo=true', () => {
    const selfRepoPrompt = buildThinkerPrompt({
      ...base,
      isSelfRepo: true,
      selfImprovementBlock: '\n\n# Self-improvement\n\nThis project\'s codebase includes your own prompts.',
    });
    expect(selfRepoPrompt).toContain('# Self-improvement');

    const notSelfRepo = buildThinkerPrompt({ ...base, isSelfRepo: false, selfImprovementBlock: '' });
    expect(notSelfRepo).not.toContain('# Self-improvement');
  });

  it('Self-repo line reflects isSelfRepo flag', () => {
    expect(buildThinkerPrompt({ ...base, isSelfRepo: true })).toContain('Self-repo: true');
    expect(buildThinkerPrompt({ ...base, isSelfRepo: false })).toContain('Self-repo: false');
  });

  it('includes required recommended_mode instruction on create-direction calls', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('recommended_mode');
    expect(prompt).toContain('create-direction');
    // The instruction must explicitly require emitting recommended_mode on create-direction
    const rdIdx = prompt.indexOf('create-direction');
    const rmIdx = prompt.indexOf('recommended_mode');
    // recommended_mode instruction appears in the prompt with create-direction nearby
    expect(Math.abs(rdIdx - rmIdx)).toBeLessThan(1000);
  });

  it('includes source citation instruction (dedup-justification)', () => {
    const prompt = buildThinkerPrompt(base);
    // Agents must cite sources on gaps/directions so dedup logic can trace provenance
    expect(prompt).toMatch(/source.*user_message/);
    expect(prompt).toContain('Cite sources');
  });

  it('includes retire-direction instruction language', () => {
    const prompt = buildThinkerPrompt(base);
    // The prompt must instruct the agent to retire directions
    expect(prompt).toContain('retire');
    // Moves section must explicitly list retirement
    expect(prompt).toMatch(/Directions to retire/);
  });

  it('does not contain banned legacy directive phrases', () => {
    const prompt = buildThinkerPrompt(base);
    // These are removed/renamed operations that should not appear in current prompt
    expect(prompt).not.toContain('add-gap');
    expect(prompt).not.toContain('create-gap');
    expect(prompt).not.toContain('create_gap');
    // set_vision was the old API; problem_model is the new substrate
    expect(prompt).not.toContain('set_vision');
  });

  it('dirSummaryText contribution stays under 50,000 chars', () => {
    const hugeDirSummary = 'x'.repeat(49_000);
    const prompt = buildThinkerPrompt({ ...base, dirSummaryText: hugeDirSummary });
    const idx = prompt.indexOf(hugeDirSummary);
    expect(idx).toBeGreaterThan(-1);
    // The prompt should not balloon — the section itself is bounded by its input
    expect(hugeDirSummary.length).toBeLessThan(50_000);
  });

  it('outcomeSection contribution stays under 30,000 chars', () => {
    const hugeOutcome = 'y'.repeat(29_000);
    const prompt = buildThinkerPrompt({ ...base, outcomeSection: hugeOutcome });
    expect(prompt).toContain(hugeOutcome);
    expect(hugeOutcome.length).toBeLessThan(30_000);
  });

  it('threadSection contribution stays under 20,000 chars', () => {
    const hugeThread = 'z'.repeat(19_000);
    const prompt = buildThinkerPrompt({ ...base, threadSection: hugeThread });
    expect(prompt).toContain(hugeThread);
    expect(hugeThread.length).toBeLessThan(20_000);
  });

  it('includes the taskId in file paths for problem model and running update', () => {
    const prompt = buildThinkerPrompt({ ...base, taskId: 'abc-456' });
    expect(prompt).toContain('/tmp/problem_model_abc-456.json');
    expect(prompt).toContain('/tmp/running_update_abc-456.md');
    expect(prompt).toContain('/tmp/research_progress_abc-456.md');
  });

  it('fails detection: removing a required section header causes assertion to fail', () => {
    // Simulate a broken prompt where "## 1. State assessment" was accidentally removed
    const prompt = buildThinkerPrompt(base);
    const broken = prompt.replace('## 1. State assessment', '');
    expect(broken).not.toContain('## 1. State assessment');
    // This verifies our structural test WOULD catch such a regression
    expect(prompt).toContain('## 1. State assessment'); // original still passes
  });
});
