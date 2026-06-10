import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
  },
}));

vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  resolveBaseBranch: (payload: Record<string, unknown>, task: Record<string, unknown>, fallback = 'main') => {
    if (typeof payload.base_branch === 'string' && payload.base_branch) return payload.base_branch;
    const meta = task.project_metadata as Record<string, unknown> | undefined;
    if (typeof meta?.integration_branch === 'string' && meta.integration_branch) return meta.integration_branch;
    return fallback;
  },
  sanitize: (s: string) => s,
  readHandlerOutput: vi.fn().mockReturnValue(null),
  log: vi.fn(),
  getAgentToken: vi.fn().mockReturnValue(''),
  apiPost: vi.fn().mockResolvedValue({}),
  apiGet: vi.fn().mockResolvedValue({}),
  getProviderRunner: vi.fn().mockReturnValue('claude'),
  // handlePlan/handleResearch read these via dynamic property access — vitest
  // browser-mode resolves them statically so they must exist on the factory.
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('child_process', () => ({
  // Route `git rev-parse` (HEAD and origin/<branch>) to a single stable sha so
  // verifyCloneDelivered sees a resolvable HEAD that matches its remote tracking
  // ref; every other git call returns empty output, which the handlers tolerate
  // via try/catch. Suites that need a specific tree override this per-test.
  execFileSync: vi.fn((_file: string, args?: unknown) => {
    const a = Array.isArray(args) ? (args as string[]) : [];
    if (a[0] === 'rev-parse') return 'deadbeefcafe0000';
    return Buffer.from('');
  }),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatMiss: vi.fn(),
    recordFormatRepairAttempt: vi.fn(),
    recordFormatRepairSuccess: vi.fn(),
  };
});

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    readClaudeMd: vi.fn().mockReturnValue(''),
    buildContextPrefix: vi.fn().mockReturnValue(''),
    buildFileTree: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    writeDesignDocHelper: vi.fn(),
    excludeAgentHelpers: vi.fn(),
    buildRetryContext: vi.fn().mockReturnValue(''),
    formatPlaybook: vi.fn().mockReturnValue(''),
  };
});

import fs from 'fs';
import { runClaudeCode, persistAndPostStatus, getProviderRunner, apiGet } from '../index.js';
import { execFileSync } from 'child_process';
import { handlePlan, handleResearch } from './research-plan.js';
import { handleStrategic } from './strategic.js';
import { writeDesignDocHelper } from '../prompt.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import { recordFormatMiss, recordFormatRepairAttempt, recordFormatRepairSuccess } from '../telemetry.js';
import {
  calcMessageAge,
  parseResearchContext,
  aggregateDirectionStats,
  buildStrategicCompletionLogs,
  classifyFailureCategory,
  buildScopeCalibrationSignal,
  buildFilteredFindingsSection,
  buildRejectedProposalsSection,
  buildOperatorOverridesSection,
  buildRunningUpdateSupersededSection,
  buildUserPausedSection,
  readProgressArtifact,
  buildPortfolioComposition,
  buildVersionPhaseSection,
  buildMemoCitationsSection,
  resolveMemoHandles,
  buildEfficiencySnapshotSection,
  buildEmptyRepoGuidance,
  buildCapabilityVerificationSection,
  buildAvailableCapabilitiesSection,
  CAPABILITY_DECLARATION_DOC,
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

describe('buildOperatorOverridesSection', () => {
  it('returns empty string for empty input', () => {
    expect(buildOperatorOverridesSection([])).toBe('');
  });

  it('renders vision/intent overrides with agent + operator values', () => {
    const out = buildOperatorOverridesSection([
      { document: 'vision', field: 'vision.goal', agent_value: 'ship fast', operator_value: 'ship safe', at: '2026-06-02T10:00:00Z' },
      { document: 'intent', field: 'intent.purpose', agent_value: 'rewrite', operator_value: 'refine', at: '2026-06-01T10:00:00Z' },
    ]);
    expect(out).toContain('## Operator Overrides Since Last Cycle');
    expect(out).toContain('do NOT simply re-propose');
    expect(out).toContain('(2026-06-02) vision `vision.goal`');
    expect(out).toContain('you proposed "ship fast"; the operator kept "ship safe".');
    expect(out).toContain('(2026-06-01) intent `intent.purpose`');
  });

  it('renders running_update overrides without an operator scalar value', () => {
    const out = buildOperatorOverridesSection([
      { document: 'running_update', field: 'running_update', agent_value: 'my rewrite', at: '2026-06-03T10:00:00Z' },
    ]);
    expect(out).toContain('(2026-06-03) running_update');
    expect(out).toContain('the operator kept their own running_update edit');
  });

  it('caps at 8 entries', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      document: 'vision', field: `f${i}`, agent_value: `a${i}`, operator_value: `o${i}`, at: '2026-06-01T00:00:00Z',
    }));
    const out = buildOperatorOverridesSection(many);
    expect(out).toContain('`f7`');
    expect(out).not.toContain('`f8`');
  });
});

describe('buildRunningUpdateSupersededSection', () => {
  it('returns empty string for null/undefined signal', () => {
    expect(buildRunningUpdateSupersededSection(null)).toBe('');
    expect(buildRunningUpdateSupersededSection(undefined)).toBe('');
  });

  it('returns empty string when count is zero or missing', () => {
    expect(buildRunningUpdateSupersededSection({ count: 0 })).toBe('');
    expect(buildRunningUpdateSupersededSection({})).toBe('');
  });

  it('renders an operator-authoritative note with the most-recent date', () => {
    const out = buildRunningUpdateSupersededSection({
      count: 2,
      most_recent_at: '2026-05-20T09:30:00Z',
    });
    expect(out).toContain('operator preserved their own running_update');
    expect(out).toContain('operator-authoritative');
    expect(out).toContain('do NOT rewrite it from a blank slate');
    expect(out).toContain('most recent: 2026-05-20');
  });

  it('renders the note without a date tail when most_recent_at is absent', () => {
    const out = buildRunningUpdateSupersededSection({ count: 1 });
    expect(out).toContain('operator preserved their own running_update');
    expect(out).not.toContain('most recent:');
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

  it('does not render "portfolio is empty" when authoritative total>0 but no rows shipped', () => {
    const out = buildPortfolioComposition([], [], undefined, 11);
    expect(out).not.toContain('portfolio is empty');
    expect(out).toContain('Portfolio has 11 active direction(s)');
    expect(out).toContain('./fetch direction');
  });

  it('still renders "portfolio is empty" when authoritative total is zero', () => {
    expect(buildPortfolioComposition([], [], undefined, 0))
      .toBe('No active directions — portfolio is empty.');
  });

  it('divides percentages by authoritative total and flags the trimmed subset', () => {
    // 2 shipped rows but the true active total is 8.
    const dirs = [
      { id: 'd-1', category: 'product', total_cycles: 1 },
      { id: 'd-2', category: 'structural', total_cycles: 1 },
    ];
    const out = buildPortfolioComposition(dirs, [], undefined, 8);
    // 1 of 8 product -> 13% (not 50% as it would be against the shipped subset).
    expect(out).toContain('product=1 (13%)');
    expect(out).toContain('Category mix below computed from 2 of 8 active directions');
    expect(out).toContain('authoritative total of 8');
  });

  it('does not report a system uncovered when its only direction was trimmed but the authoritative map covers it', () => {
    // Only sys-a directions shipped; sys-b's lone direction was trimmed for
    // budget but the authoritative per-system map records it as active.
    const dirs = [
      { id: 'd-1', category: 'product', system_id: 'sys-a', total_cycles: 1 },
    ];
    const systems = [
      { id: 'sys-a', name: 'System A' },
      { id: 'sys-b', name: 'System B' },
    ];
    const out = buildPortfolioComposition(dirs, systems, undefined, 4, { 'sys-a': 3, 'sys-b': 1 });
    // sys-b has an active direction per the authoritative map → not a gap.
    expect(out).toContain('System coverage: 2/2 vision systems have directions');
    expect(out).not.toContain('System B (sys-b)');
    expect(out).not.toContain('gaps:');
  });

  it('derives concentration risk from the authoritative map even when shipped rows show <3', () => {
    // Only one sys-a row shipped, but the authoritative map says 4 are active.
    const dirs = [
      { id: 'd-1', category: 'product', system_id: 'sys-a', total_cycles: 1 },
    ];
    const out = buildPortfolioComposition(dirs, [], undefined, 4, { 'sys-a': 4 });
    expect(out).toContain('CONCENTRATION RISK: 3+ directions on same system: sys-a=4');
    expect(out).not.toContain('may under-count');
  });

  it('still flags a truly-uncovered system (authoritative active count 0)', () => {
    const dirs = [
      { id: 'd-1', category: 'product', system_id: 'sys-a', total_cycles: 1 },
    ];
    const systems = [
      { id: 'sys-a', name: 'System A' },
      { id: 'sys-b', name: 'System B' },
    ];
    const out = buildPortfolioComposition(dirs, systems, undefined, 1, { 'sys-a': 1 });
    expect(out).toContain('System coverage: 1/2 vision systems have directions');
    expect(out).toContain('System B (sys-b)');
  });

  it('suppresses false uncovered claim and marks concentration partial when map absent and list trimmed', () => {
    // 1 shipped row but authoritative total is 5 (trimmed), no per-system map.
    const dirs = [
      { id: 'd-1', category: 'product', system_id: 'sys-a', total_cycles: 1 },
      { id: 'd-2', category: 'structural', system_id: 'sys-a', total_cycles: 1 },
      { id: 'd-3', category: 'structural', system_id: 'sys-a', total_cycles: 1 },
    ];
    const systems = [
      { id: 'sys-a', name: 'System A' },
      { id: 'sys-b', name: 'System B' },
    ];
    const out = buildPortfolioComposition(dirs, systems, undefined, 5);
    // No false "uncovered" gap line for sys-b — its direction may be trimmed.
    expect(out).toContain('System coverage: at least 1/2 vision systems have directions');
    expect(out).toContain('coverage gaps not shown');
    expect(out).not.toContain('System B (sys-b)');
    // Concentration computed from shipped subset only — clearly marked partial.
    expect(out).toContain('CONCENTRATION RISK: 3+ directions on same system: sys-a=3');
    expect(out).toContain('may under-count trimmed directions');
  });

  it('preserves exact current behavior when map absent and list complete', () => {
    // No authoritative map, and shipped rows == authoritative total (untrimmed).
    const dirs = [
      { id: 'd-1', text: 'Product A', category: 'product', system_id: 'sys-a', total_cycles: 2 },
      { id: 'd-2', text: 'Refactor A', category: 'structural', system_id: 'sys-a', total_cycles: 1 },
      { id: 'd-3', text: 'More A', category: 'structural', system_id: 'sys-a', total_cycles: 1 },
    ];
    const systems = [
      { id: 'sys-a', name: 'System A' },
      { id: 'sys-b', name: 'System B' },
    ];
    const out = buildPortfolioComposition(dirs, systems, undefined, 3);
    expect(out).toContain('System coverage: 1/2 vision systems have directions');
    expect(out).toContain('System B (sys-b)');
    expect(out).toContain('CONCENTRATION RISK: 3+ directions on same system: sys-a=3');
    expect(out).not.toContain('may under-count');
    expect(out).not.toContain('coverage gaps not shown');
  });
});

describe('buildEmptyRepoGuidance', () => {
  // buildEmptyRepoGuidance now routes through the unified isGreenfieldRepo
  // predicate, which ORs the durable repo_is_empty signal with the live
  // detectGreenfield() clone scan. To isolate the durable-signal cases we must
  // control what the live scan sees via the git ls-files mock.
  const setSourceRichClone = () =>
    vi.mocked(execFileSync).mockReturnValue(
      // > GREENFIELD_SOURCE_FILE_THRESHOLD (8) real source files → live scan false
      Array.from({ length: 12 }, (_, i) => `src/m${i}.ts`).join('\n') as never,
    );
  const setEmptyClone = () =>
    vi.mocked(execFileSync).mockReturnValue('package.json\nREADME.md\n.gitignore' as never);

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns the bootstrap paragraph when repo_is_empty is truthy', () => {
    // Durable signal short-circuits even when the live clone has real source.
    setSourceRichClone();
    const out = buildEmptyRepoGuidance({ repo_is_empty: true });
    expect(out).toContain('If the codebase is nearly empty');
    expect(out).toContain('bootstraps the target system');
  });

  it('returns empty string when repo_is_empty is false and the live clone has source', () => {
    setSourceRichClone();
    expect(buildEmptyRepoGuidance({ repo_is_empty: false })).toBe('');
  });

  it('returns empty string when repo_is_empty is absent and the live clone has source', () => {
    setSourceRichClone();
    expect(buildEmptyRepoGuidance({})).toBe('');
  });

  it('returns empty string for non-boolean falsy values when the live clone has source', () => {
    setSourceRichClone();
    expect(buildEmptyRepoGuidance({ repo_is_empty: 0 })).toBe('');
    expect(buildEmptyRepoGuidance({ repo_is_empty: '' })).toBe('');
  });

  it('returns the bootstrap paragraph when repo_is_empty is absent but the live clone is near-empty', () => {
    // Durable signal absent → the live full-clone scan catches a
    // lightly-scaffolded repo. Both handlers agree on this.
    setEmptyClone();
    const out = buildEmptyRepoGuidance({});
    expect(out).toContain('If the codebase is nearly empty');
  });

  it('returns empty string when repo_is_empty is explicitly false but the live clone is near-empty', () => {
    // The durable signal is authoritative: a durable "non-empty" reading paired
    // with an empty live tree is a clone problem (handled by verifyCloneDelivered),
    // NOT a greenfield repo. It must never silently produce bootstrap guidance.
    setEmptyClone();
    expect(buildEmptyRepoGuidance({ repo_is_empty: false })).toBe('');
  });
});

describe('buildCapabilityVerificationSection', () => {
  it('returns empty string when the verification map is absent', () => {
    expect(buildCapabilityVerificationSection(undefined)).toBe('');
  });

  it('returns empty string for an empty verification map', () => {
    expect(buildCapabilityVerificationSection({})).toBe('');
  });

  it('flags a working-status capability whose every backing path is absent', () => {
    // claimed_absent is the backend verdict for a capability claimed working/partial
    // whose declared backing paths are ALL missing on the integration branch.
    const section = buildCapabilityVerificationSection({
      'streaming-uploads': {
        status: 'claimed_absent',
        claimed_status: 'working',
        missing: ['backend/app/streaming.py', 'agent/src/upload.ts'],
        present: [],
      },
    });
    expect(section).toContain('# CAPABILITY VERIFICATION');
    expect(section).toContain('Claimed-but-absent');
    expect(section).toContain('**streaming-uploads**');
    expect(section).toContain('design-doc status: working');
    expect(section).toContain('CLAIMED-BUT-ABSENT');
    expect(section).toContain('backend/app/streaming.py');
    expect(section).toContain('agent/src/upload.ts');
  });

  it('does not flag a capability that partially exists (a present backing path)', () => {
    // A capability with at least one present backing path is conservatively
    // classified `partial`, not claimed_absent — it must NOT appear under the
    // "Claimed-but-absent" heading.
    const section = buildCapabilityVerificationSection({
      'token-refresh': {
        status: 'partial',
        claimed_status: 'working',
        missing: ['backend/app/refresh.py'],
        present: ['agent/src/auth.ts'],
      },
    });
    expect(section).toContain('PARTIAL');
    expect(section).toContain('**token-refresh**');
    expect(section).toContain('present: agent/src/auth.ts');
    // Present-path capabilities are left alone — never marked claimed-but-absent.
    expect(section).not.toContain('Claimed-but-absent');
    expect(section).not.toContain('CLAIMED-BUT-ABSENT');
  });

  it('leaves a capability with no path pointer untouched (never classified)', () => {
    // The backend never classifies a capability that declares no backing path
    // (absence of a path is not evidence of absent code), so it never appears in
    // the verification map. A map containing only such a (never-emitted)
    // capability is effectively empty here — the section renders nothing for it.
    const section = buildCapabilityVerificationSection({
      'verified-cap': {
        status: 'verified',
        claimed_status: 'working',
        missing: [],
        present: ['backend/app/real.py'],
      },
    });
    // The pathless capability is simply not in the map; only the verified one is.
    expect(section).toContain('Verified');
    expect(section).toContain('**verified-cap**');
    expect(section).not.toContain('Claimed-but-absent');
    expect(section).not.toContain('PARTIAL');
  });

  it('groups mixed verdicts into their respective sections', () => {
    const section = buildCapabilityVerificationSection({
      'cap-absent': { status: 'claimed_absent', claimed_status: 'working', missing: ['a.py'], present: [] },
      'cap-partial': { status: 'partial', claimed_status: 'partial', missing: ['b.py'], present: ['c.ts'] },
      'cap-verified': { status: 'verified', claimed_status: 'mature', missing: [], present: ['d.py'] },
    });
    expect(section).toContain('Claimed-but-absent');
    expect(section).toContain('**cap-absent**');
    expect(section).toContain('Partial');
    expect(section).toContain('**cap-partial**');
    expect(section).toContain('Verified');
    expect(section).toContain('**cap-verified**');
  });
});

describe('buildAvailableCapabilitiesSection (shared vocabulary)', () => {
  it('returns empty string when the field is absent or empty', () => {
    expect(buildAvailableCapabilitiesSection(undefined)).toBe('');
    expect(buildAvailableCapabilitiesSection([])).toBe('');
  });

  it('renders canonical names with statuses and the draw-from-registry framing', () => {
    const section = buildAvailableCapabilitiesSection([
      { name: 'payment-core', status: 'working' },
      { name: 'seeded-eval-harness', status: 'planned' },
    ]);
    // Header + the load-bearing "shared vocabulary" / draw-from-list framing.
    expect(section).toContain('AVAILABLE CAPABILITIES');
    expect(section).toContain('shared vocabulary');
    expect(section).toMatch(/DRAW THE NAME FROM THIS LIST/);
    // Canonical names and their statuses both render.
    expect(section).toContain('`payment-core`');
    expect(section).toContain('(working)');
    expect(section).toContain('`seeded-eval-harness`');
    expect(section).toContain('(planned)');
    // The matcher is framed as exact, never fuzzy.
    expect(section).toContain('EXACT');
  });

  it('renders a name without status when status is omitted, and skips blank entries', () => {
    const section = buildAvailableCapabilitiesSection([
      { name: 'auth-core' },
      { name: '' },
      { status: 'working' } as Record<string, unknown>,
    ]);
    expect(section).toContain('`auth-core`');
    // No status parens for the status-less entry on its own line.
    expect(section).toMatch(/- `auth-core`\n/);
  });

  it('renders proof_status alongside status across proven / unproven / partial / planned', () => {
    const section = buildAvailableCapabilitiesSection([
      { name: 'payment-core', status: 'working', proof_status: 'proven' },
      { name: 'fraud-detect', status: 'working', proof_status: 'unproven' },
      { name: 'risk-scoring', status: 'partial', proof_status: 'unproven' },
      { name: 'seeded-eval-harness', status: 'planned', proof_status: 'unproven' },
    ]);
    // proven renders as the lone second qualifier next to its declared status.
    expect(section).toContain('`payment-core` (working, proven)');
    // working-but-unproven is visibly distinct from proven.
    expect(section).toContain('`fraud-detect` (working, unproven)');
    expect(section).toContain('`risk-scoring` (partial, unproven)');
    expect(section).toContain('`seeded-eval-harness` (planned, unproven)');
    // A one-line legend distinguishing the two axes is present.
    expect(section).toContain('proof_status');
    expect(section).toMatch(/evidence-grounded proof records exist/);
  });

  it('renders proof_status without a declared status as the sole qualifier', () => {
    const section = buildAvailableCapabilitiesSection([
      { name: 'audit-gate', proof_status: 'proven' },
    ]);
    expect(section).toContain('`audit-gate` (proven)');
  });

  it('omits the proof legend and renders status-only when no entry carries proof_status', () => {
    const section = buildAvailableCapabilitiesSection([
      { name: 'payment-core', status: 'working' },
      { name: 'seeded-eval-harness', status: 'planned' },
    ]);
    // Back-compat: status-only rendering is unchanged when proof_status is absent.
    expect(section).toContain('`payment-core` (working)');
    expect(section).toContain('`seeded-eval-harness` (planned)');
    expect(section).not.toContain('proof_status');
  });
});

describe('CAPABILITY_DECLARATION_DOC (uniform across task types)', () => {
  it('documents both the provides_capability stamp and the prerequisite ref drawn from the registry', () => {
    expect(CAPABILITY_DECLARATION_DOC).toContain('provides_capability');
    expect(CAPABILITY_DECLARATION_DOC).toContain('prerequisite');
    expect(CAPABILITY_DECLARATION_DOC).toContain('AVAILABLE CAPABILITIES');
    // Applies to every task type, not research special-casing.
    expect(CAPABILITY_DECLARATION_DOC).toContain('every other task type');
    // Off-registry refs are accepted (advisory), never rejected.
    expect(CAPABILITY_DECLARATION_DOC).toMatch(/never rejected/);
  });
});

describe('buildVersionPhaseSection', () => {
  it('returns empty string when versionPhase is undefined', () => {
    expect(buildVersionPhaseSection(undefined)).toBe('');
  });

  it('returns empty string when versionPhase is missing required fields', () => {
    expect(buildVersionPhaseSection({})).toBe('');
    expect(buildVersionPhaseSection({ version_number: 1 })).toBe('');
    expect(buildVersionPhaseSection({ phase: 'build' })).toBe('');
  });

  it('returns empty string when phase is an invalid value', () => {
    expect(buildVersionPhaseSection({ version_number: 1, phase: 'unknown' })).toBe('');
  });

  it('renders build phase with version number', () => {
    const result = buildVersionPhaseSection({ version_number: 3, phase: 'build' });
    expect(result).toContain('v3');
    expect(result).toContain('BUILD');
    expect(result).toContain('Version Phase');
  });

  it('renders stabilize phase with version number', () => {
    const result = buildVersionPhaseSection({ version_number: 5, phase: 'stabilize' });
    expect(result).toContain('v5');
    expect(result).toContain('STABILIZE');
  });

  it('includes phase_changed_at when present', () => {
    const result = buildVersionPhaseSection({ version_number: 2, phase: 'build', phase_changed_at: '2026-05-10T12:00:00Z' });
    expect(result).toContain('2026-05-10T12:00:00Z');
  });

  it('includes tasks_merged_this_version when present', () => {
    const result = buildVersionPhaseSection({ version_number: 2, phase: 'build', tasks_merged_this_version: 42 });
    expect(result).toContain('42');
    expect(result).toContain('Tasks merged this version');
  });

  it('renders last_qualification object as a compact summary line (status/confidence/sha/blocker)', () => {
    // The backend ALWAYS sends last_qualification as an object (the structured
    // qualification record) or null — never a string. The summary must surface
    // status, confidence, candidate_sha, and blocker.
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.61,
        candidate_sha: 'abcdef0123456789',
        blocker: 'test_file_deleted',
      },
    });
    expect(result).toContain('Last qualification:');
    expect(result).toContain('status=not_promoted');
    expect(result).toContain('confidence=0.61');
    // candidate_sha is truncated to 12 chars
    expect(result).toContain('candidate_sha=abcdef012345');
    expect(result).toContain('blocker=test_file_deleted');
  });

  it('renders sub-scores and audit findings for the current last_qualification, including the binding dimension', () => {
    // No hard blocker and smoketest enabled (flag absent → enabled): the binding
    // constraint is the lowest numeric sub-score.
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.61,
        candidate_sha: 'abcdef0123456789',
        sub_scores: {
          audit: 0.74,
          test_erosion: 0.42,
          smoketest: 0.88,
        },
        audit_findings: [
          { severity: 'low', summary: 'documentation drift' },
          { severity: 'critical', summary: 'unsafe release path' },
          { severity: 'high', summary: 'broken retry guard' },
          { severity: 'medium', summary: 'missing null check' },
        ],
      },
    });
    expect(result).toContain('sub-scores: audit=0.74, test_erosion=0.42, smoketest=0.88 (binding=test_erosion=0.42)');
    expect(result).toContain('Audit findings: critical: unsafe release path; high: broken retry guard; medium: missing null check (+1 more)');
  });

  it('renders the excluded-audit-findings caveat and section when findings were excluded for lacking verification', () => {
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.93,
        sub_scores: { audit: 0.98, test_erosion: 1.0, smoketest: 0.95 },
        // Scored findings list is empty (all higher-severity findings excluded).
        audit_findings: [],
        excluded_audit_findings: [
          { severity: 'high', summary: 'broken retry guard', unverified: 'true' },
          { severity: 'critical', summary: 'unsafe release path', unverified: 'true' },
          { severity: 'medium', summary: 'missing null check', unverified: 'true' },
          { severity: 'medium', summary: 'second medium', unverified: 'true' },
        ],
        audit_reconciliation: {
          model_reported_score: 0.70,
          findings_derived_score: 0.98,
          score_delta: 0.28,
          excluded_findings_count: 4,
        },
      },
    });
    // Part (1): the audit-score caveat names the excluded count and the divergence.
    expect(result).toContain('Audit score caveat: audit sub-score reflects only verified findings; 4 higher-severity findings excluded for lacking verification; model-reported audit 0.70 vs findings-derived 0.98 (Δ0.28)');
    // Part (2): the excluded findings are rendered, clearly marked unverified, sorted most-severe first and capped at 3.
    expect(result).toContain('Excluded audit findings (UNVERIFIED — not confirmed blockers): critical: unsafe release path; high: broken retry guard; medium: missing null check (+1 more)');
  });

  it('omits the excluded-findings caveat and section when nothing was excluded', () => {
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.61,
        sub_scores: { audit: 0.74, test_erosion: 0.42, smoketest: 0.88 },
        excluded_audit_findings: [],
        audit_reconciliation: {
          model_reported_score: 0.74,
          findings_derived_score: 0.74,
          score_delta: 0.0,
          excluded_findings_count: 0,
        },
      },
    });
    expect(result).not.toContain('Audit score caveat:');
    expect(result).not.toContain('Excluded audit findings');
  });

  it('renders test-erosion detail (deleted files + negative deltas) when carried', () => {
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.4,
        test_erosion_detail: {
          deleted_test_files: ['testing/test_foo.py', 'testing/test_bar.py'],
          deleted_test_files_truncated: 3,
          test_count_delta: -4,
          assertion_delta: -12,
        },
      },
    });
    expect(result).toContain('Test erosion: deleted testing/test_foo.py, testing/test_bar.py (+3 more); -4 tests; -12 assertions');
  });

  it('renders smoketest detail (failed suite + named failing tests) when carried', () => {
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.4,
        smoketest_detail: {
          startup_failed: true,
          failed_suite: 'startup',
          failed_tests: ['boot_check', 'health_probe'],
          failed_tests_truncated: 2,
          passed: 1,
          total: 5,
        },
      },
    });
    expect(result).toContain('Smoketest: startup suite failed; failed: boot_check, health_probe (+2 more)');
  });

  it('omits test-erosion and smoketest detail lines when absent', () => {
    const result = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.61,
        test_erosion_detail: null,
        smoketest_detail: null,
        sub_scores: { audit: 0.74, test_erosion: 0.42, smoketest: 0.88 },
      },
    });
    expect(result).not.toContain('Test erosion:');
    expect(result).not.toContain('Smoketest:');
  });

  it('excludes a disabled smoketest dimension from the sub-scores line and binding selection', () => {
    // smoketest_enabled=false → the smoketest 0.0 is a placeholder, not a real
    // low score. It must not appear in the sub-scores line, and must never be
    // chosen as the binding constraint even though its placeholder is the min.
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      last_qualification: {
        status: 'promoted',
        confidence: 0.93,
        smoketest_enabled: false,
        sub_scores: {
          audit: 0.95,
          test_erosion: 1.0,
          smoketest: 0.0,
        },
      },
    });
    expect(result).toContain('sub-scores: audit=0.95, test_erosion=1.00 (binding=audit=0.95)');
    expect(result).not.toContain('smoketest=');
    expect(result).not.toContain('binding=smoketest');
  });

  it('reports a hard CI blocker (smoketest disabled) as the binding constraint, not a numeric minimum', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0,
        smoketest_enabled: false,
        blocker: 'ci_not_green',
        sub_scores: {
          audit: 0.0,
          test_erosion: 0.0,
          smoketest: 0.0,
        },
      },
    });
    expect(result).toContain('binding=ci_not_green');
    // The numeric-minimum binding form must NOT appear under a hard blocker.
    expect(result).not.toContain('binding=audit=');
    expect(result).not.toContain('binding=test_erosion=');
    // Disabled smoketest stays excluded from the rendered sub-scores.
    expect(result).not.toContain('smoketest=');
  });

  it('reports a test_file_deleted blocker as the binding constraint even when smoketest is enabled', () => {
    const result = buildVersionPhaseSection({
      version_number: 5,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0,
        blocker: 'test_file_deleted',
        sub_scores: {
          audit: 0.81,
          test_erosion: 0.0,
          smoketest: 0.62,
        },
      },
    });
    expect(result).toContain('sub-scores: audit=0.81, test_erosion=0.00, smoketest=0.62 (binding=test_file_deleted)');
    expect(result).not.toContain('binding=test_erosion=');
  });

  it('surfaces CI blocker provenance (state and detail) alongside the blocker code', () => {
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0,
        blocker: 'ci_unresolved',
        ci_resolution: {
          state: 'PENDING',
          resolved_sha: 'abc123',
          detail: 'build not started',
        },
        sub_scores: {
          audit: 0.9,
          test_erosion: 0.9,
          smoketest: 0.9,
        },
      },
    });
    expect(result).toContain('binding=ci_unresolved');
    expect(result).toContain('state: PENDING');
    expect(result).toContain('build not started');
  });

  it('surfaces audit blocker provenance (missing reason) alongside the blocker code', () => {
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0,
        blocker: 'audit_missing',
        audit_missing_reason: 'empty_report',
        sub_scores: {
          audit: null,
          test_erosion: 0.9,
          smoketest: 0.9,
        },
      },
    });
    expect(result).toContain('binding=audit_missing');
    expect(result).toContain('reason: empty_report');
  });

  it('identifies a genuine low audit score as the binding constraint (smoketest disabled, no blocker)', () => {
    const result = buildVersionPhaseSection({
      version_number: 6,
      phase: 'stabilize',
      last_qualification: {
        status: 'not_promoted',
        confidence: 0.55,
        smoketest_enabled: false,
        sub_scores: {
          audit: 0.41,
          test_erosion: 0.92,
          smoketest: 0.0,
        },
      },
    });
    expect(result).toContain('sub-scores: audit=0.41, test_erosion=0.92 (binding=audit=0.41)');
    expect(result).not.toContain('smoketest=');
  });

  it('renders a pending last_qualification and instructs the agent NOT to re-emit qualify_request', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'stabilize',
      last_qualification: { status: 'pending', candidate_sha: 'deadbeefcafe0000' },
    });
    expect(result).toContain('status=pending');
    // Inline guidance next to the pending status line.
    expect(result).toContain('PENDING');
    expect(result).toMatch(/do NOT emit `?qualify_request`?/i);
  });

  it('omits the pending warning when last_qualification is finalized (not pending)', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'stabilize',
      last_qualification: { status: 'promoted', candidate_sha: 'feedface00000000' },
    });
    expect(result).toContain('status=promoted');
    expect(result).not.toContain('currently **PENDING**');
  });

  it('renders a dry_run last_qualification and instructs the agent NOT to re-emit qualify_request', () => {
    // A finalized dry-run already fully evaluated its candidate. Re-emitting
    // qualify_request for the SAME candidate re-spawns agent VMs for nothing
    // (the orchestrator's cached_verdict guard ignores it) — the prompt must
    // discourage it. H1: extends the pending-only guard to conclusive verdicts.
    const result = buildVersionPhaseSection({
      version_number: 6,
      phase: 'stabilize',
      last_qualification: {
        status: 'dry_run',
        candidate_sha: 'abcdef0123456789',
        confidence: 0.91,
        would_promote: true,
      },
    });
    expect(result).toContain('status=dry_run');
    // Surfaces would_promote so the agent sees the candidate passed.
    expect(result).toContain('would_promote=true');
    // Inline guidance next to the dry_run status line, with the SHA rendered.
    expect(result).toContain('DRY RUN');
    expect(result).toContain('abcdef012345');
    expect(result).toMatch(/do NOT emit `?qualify_request`?/i);
    // The instruction is conditional on the candidate being unchanged.
    expect(result).toMatch(/UNLESS new work has since merged|candidate commit has changed/i);
  });

  it('does not render the pending warning for a dry_run last_qualification', () => {
    const result = buildVersionPhaseSection({
      version_number: 6,
      phase: 'stabilize',
      last_qualification: { status: 'dry_run', candidate_sha: 'abcdef0123456789' },
    });
    expect(result).not.toContain('currently **PENDING**');
    // ...but the dry_run-specific guidance IS present.
    expect(result).toContain('DRY RUN');
  });

  it('includes prompt-level guidance not to re-request after a dry-run of the same candidate', () => {
    // Prompt-level guidance (independent of whether a dry_run record is passed).
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'stabilize' });
    expect(result).toContain('Do NOT re-request after a dry-run of the same candidate');
    expect(result).toContain('status=dry_run');
  });

  it('does not emit the dry_run guidance for a finalized non-dry_run qualification', () => {
    // not_promoted is finalized but not a dry_run — the inline dry_run line
    // must not appear (the bottom-of-prompt static guidance still mentions it).
    const result = buildVersionPhaseSection({
      version_number: 7,
      phase: 'stabilize',
      last_qualification: { status: 'not_promoted', candidate_sha: 'cafe000011112222', blocker: 'ci_red' },
    });
    expect(result).toContain('status=not_promoted');
    expect(result).not.toContain('The last qualification was a **DRY RUN**');
  });

  it('ignores a non-object last_qualification (string/array) without crashing', () => {
    // Defensive: even though the backend never sends a string, a malformed
    // payload must not throw or render a garbage line.
    const strForm = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: '2026-05-15T08:00:00Z',
    });
    expect(strForm).toContain('## Version Phase');
    expect(strForm).not.toContain('Last qualification:');
    const arrForm = buildVersionPhaseSection({
      version_number: 2,
      phase: 'stabilize',
      last_qualification: ['x'],
    });
    expect(arrForm).not.toContain('Last qualification:');
  });

  it('omits the Last qualification line entirely when last_qualification is absent/null', () => {
    const absent = buildVersionPhaseSection({ version_number: 2, phase: 'build' });
    expect(absent).not.toContain('Last qualification:');
    const nullForm = buildVersionPhaseSection({ version_number: 2, phase: 'build', last_qualification: null });
    expect(nullForm).not.toContain('Last qualification:');
  });

  it('instructs agent NOT to re-request qualification while one is in flight', () => {
    // Prompt-level guidance (independent of whether a pending record is passed).
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'stabilize' });
    expect(result).toContain('Do NOT re-request while a qualification is in flight');
    expect(result).toContain('status=pending');
  });

  it('explains both phase meanings', () => {
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'build' });
    expect(result).toContain('build');
    expect(result).toContain('stabilize');
    expect(result).toContain('audit');
    expect(result).toContain('hardening');
  });

  it('renders the version goal when present', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'build',
      goal: 'Make the admin UI mobile-responsive',
    });
    expect(result).toContain('Goal: Make the admin UI mobile-responsive');
  });

  it('omits the Goal line when goal is absent or empty', () => {
    const absent = buildVersionPhaseSection({ version_number: 4, phase: 'build' });
    expect(absent).not.toContain('Goal:');
    const blank = buildVersionPhaseSection({ version_number: 4, phase: 'build', goal: '   ' });
    expect(blank).not.toContain('Goal:');
  });

  it('instructs agent to emit version_phase_decision when flipping', () => {
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'build' });
    expect(result).toContain('version_phase_decision');
    expect(result).toContain('reasoning');
  });

  it('instructs agent to omit version_phase_decision when staying in current phase', () => {
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'build' });
    expect(result).toMatch(/omit|no change|do NOT emit/i);
  });

  it('renders promotion_readiness block when present', () => {
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      promotion_readiness: {
        ready: true,
        would_promote_rate: 0.9,
        consecutive_clean_passes: 4,
        verdicts_evaluated: 5,
        reason: 'qualifier looks trustworthy',
        recurring_blockers: {},
        confidence: { mean: 0.87, stdev: 0.03, stable: true },
      },
    });
    expect(result).toContain('Promotion readiness');
    expect(result).toContain('ready=YES');
    expect(result).toContain('would_promote_rate=0.9');
    expect(result).toContain('consecutive_clean_passes=4');
    expect(result).toContain('verdicts_evaluated=5');
    expect(result).toContain('qualifier looks trustworthy');
    expect(result).toContain('confidence_mean=0.87');
    expect(result).toContain('stable');
  });

  it('renders recurring_blockers in promotion_readiness when present', () => {
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      promotion_readiness: {
        ready: false,
        would_promote_rate: 0.3,
        consecutive_clean_passes: 0,
        verdicts_evaluated: 5,
        reason: 'recurring hard blocker: ci_red',
        recurring_blockers: { ci_red: 3 },
        confidence: { mean: 0.5, stdev: 0.1, stable: false },
      },
    });
    expect(result).toContain('ready=NO');
    expect(result).toContain('Recurring blockers');
    expect(result).toContain('ci_redx3');
  });

  it('omits promotion_readiness block gracefully when absent', () => {
    const result = buildVersionPhaseSection({ version_number: 1, phase: 'build' });
    expect(result).not.toContain('Promotion readiness');
    expect(result).not.toContain('Recurring blockers');
    expect(result).toContain('Version Phase');
  });

  it('renders recent_versions list when present', () => {
    const result = buildVersionPhaseSection({
      version_number: 3,
      phase: 'stabilize',
      recent_versions: [
        {
          version_number: 2,
          status: 'promoted',
          last_qualification: {
            status: 'promoted',
            confidence: 0.91,
            sub_scores: { audit: 0.86, test_erosion: 0.73, smoketest: 0.49 },
            audit_findings: [{ severity: 'high', summary: 'flaky audit step' }],
          },
        },
        { version_number: 1, status: 'promoted', last_qualification: { status: 'promoted', confidence: 0.85 } },
      ],
    });
    expect(result).toContain('Recent versions');
    expect(result).toContain('v2');
    expect(result).toContain('promoted');
    expect(result).toContain('qual=promoted');
    expect(result).toContain('confidence=0.91');
    expect(result).toContain('binding=smoketest=0.49');
    expect(result).toContain('Audit findings: high: flaky audit step');
    expect(result).toContain('v1');
  });

  it('renders recent_versions qualification details with smoketest as the binding dimension', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'stabilize',
      recent_versions: [
        {
          version_number: 3,
          status: 'promoted',
          last_qualification: {
            status: 'not_promoted',
            confidence: 0.5,
            sub_scores: { audit: 0.71, test_erosion: 0.64, smoketest: 0.31 },
            audit_findings: [{ severity: 'medium', summary: 'startup-suite regression' }],
          },
        },
      ],
    });
    expect(result).toContain('binding=smoketest=0.31');
    expect(result).toContain('Audit findings: medium: startup-suite regression');
  });

  it('omits recent_versions gracefully when absent', () => {
    const result = buildVersionPhaseSection({ version_number: 2, phase: 'stabilize' });
    expect(result).not.toContain('Recent versions');
    expect(result).toContain('Version Phase');
  });

  it('renders recent_versions blocker when present', () => {
    const result = buildVersionPhaseSection({
      version_number: 4,
      phase: 'stabilize',
      recent_versions: [
        { version_number: 3, status: 'promoted', last_qualification: { status: 'not_promoted', confidence: 0.5, blocker: 'ci_red' } },
      ],
    });
    expect(result).toContain('blocker=ci_red');
  });
});

describe('buildMemoCitationsSection', () => {
  it('returns empty string when memo citations are absent', () => {
    expect(buildMemoCitationsSection(undefined)).toBe('');
    expect(buildMemoCitationsSection(null)).toBe('');
  });

  it('returns empty string for a non-array value', () => {
    expect(buildMemoCitationsSection({ task_id: 'x' })).toBe('');
    expect(buildMemoCitationsSection('memo')).toBe('');
  });

  it('returns empty string for an empty array', () => {
    expect(buildMemoCitationsSection([])).toBe('');
  });

  it('renders a header and one bullet per memo with handle, task_id, and excerpt', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa', summary_excerpt: 'Caching layer is the bottleneck.' },
      { task_id: 'memo-bbb', summary_excerpt: 'Auth retries mask a real timeout.' },
    ]);
    expect(result).toContain('## Memo Citations');
    expect(result).toContain('M0 (task_id: memo-aaa): Caching layer is the bottleneck.');
    expect(result).toContain('M1 (task_id: memo-bbb): Auth retries mask a real timeout.');
  });

  it('assigns stable handles (M0, M1, M2, ...) based on position in array', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-first', summary_excerpt: 'First memo.' },
      { task_id: 'memo-second', summary_excerpt: 'Second memo.' },
      { task_id: 'memo-third', summary_excerpt: 'Third memo.' },
    ]);
    expect(result).toContain('M0 (task_id: memo-first)');
    expect(result).toContain('M1 (task_id: memo-second)');
    expect(result).toContain('M2 (task_id: memo-third)');
  });

  it('instructs the reviewer to cite memos by short handle', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa', summary_excerpt: 'A finding.' },
    ]);
    expect(result).toMatch(/cite it by.*handle/i);
    expect(result).toMatch(/memo M0|memo M1/);
  });

  it('keeps real task_id visible in digest for traceability', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa-uuid-12345', summary_excerpt: 'A finding.' },
    ]);
    expect(result).toContain('task_id: memo-aaa-uuid-12345');
  });

  it('falls back gracefully when an entry is missing fields', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-ccc' },
      { summary_excerpt: 'no id here' },
      'not-an-object',
    ]);
    expect(result).toContain('M0 (task_id: memo-ccc): (no summary)');
    expect(result).toContain('M1 (task_id: ?): no id here');
  });

  it('reframes memos as cross-cycle institutional memory (not budget scraps)', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa', summary_excerpt: 'A finding.' },
    ]);
    expect(result).toContain('cross-cycle institutional memory');
    expect(result).toContain('durable');
    expect(result).toContain('why findings were set aside');
    expect(result).toContain('why directions were retired');
    // Should NOT contain the old "budget pressure" framing
    expect(result).not.toMatch(/dropped.*budget|budget.*pressure/i);
  });

  it('explicitly prompts agent to actively check memos for relevance', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa', summary_excerpt: 'A finding.' },
    ]);
    expect(result).toMatch(/actively check.*memo.*relevant/i);
    expect(result).toMatch(/as you reason about gaps and directions/i);
  });

  it('emphasizes authentic citation over performative citation', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-aaa', summary_excerpt: 'A finding.' },
    ]);
    expect(result).toMatch(/authentic|genuinely informs/i);
    expect(result).toMatch(/performative.*not|valuable citations.*strengthen/i);
  });

  it('renders reasoning_kind and direction_id when present', () => {
    const result = buildMemoCitationsSection([
      {
        task_id: 'memo-ret-1',
        summary_excerpt: 'Direction retired.',
        reasoning_kind: 'direction_retirement',
        direction_id: 'dir-abc123',
      },
    ]);
    expect(result).toContain('task_id: memo-ret-1');
    expect(result).toContain('kind: direction_retirement');
    expect(result).toContain('direction: dir-abc123');
  });

  it('omits reasoning_kind and direction_id meta parts when absent (legacy shape)', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-legacy', summary_excerpt: 'Legacy memo.' },
    ]);
    expect(result).toContain('M0 (task_id: memo-legacy): Legacy memo.');
    expect(result).not.toContain('kind:');
    expect(result).not.toContain('direction:');
  });

  it('renders tags when present', () => {
    const result = buildMemoCitationsSection([
      {
        task_id: 'memo-tags',
        summary_excerpt: 'Tagged memo.',
        tags: ['retirement', 'acceptance'],
      },
    ]);
    expect(result).toContain('tags: retirement, acceptance');
  });

  it('renders success_evidence and retirement_condition as indented lines', () => {
    const result = buildMemoCitationsSection([
      {
        task_id: 'memo-ev',
        summary_excerpt: 'Evidence memo.',
        success_evidence: 'All tests passed.',
        retirement_condition: 'KPIs met for 2 cycles.',
      },
    ]);
    expect(result).toContain('success_evidence: All tests passed.');
    expect(result).toContain('retirement_condition: KPIs met for 2 cycles.');
  });

  it('handles enriched entry with all new fields present', () => {
    const result = buildMemoCitationsSection([
      {
        task_id: 'memo-full',
        summary_excerpt: 'Full enriched memo.',
        reasoning_kind: 'direction_retirement',
        direction_id: 'dir-xyz',
        tags: ['retirement'],
        citation_refs: 'direction:dir-xyz, task:memo-src',
        success_evidence: 'Evidence here.',
        retirement_condition: 'Condition here.',
        final_metrics_excerpt: 'final_prs_merged=12, final_cycles_active=4',
        rationale_excerpt: 'Retired after acceptance criteria were met.',
        reasoning_excerpt: 'Direction: full scope shipped.\n\nRetirement rationale: done.',
      },
    ]);
    expect(result).toContain('M0');
    expect(result).toContain('task_id: memo-full');
    expect(result).toContain('kind: direction_retirement');
    expect(result).toContain('direction: dir-xyz');
    expect(result).toContain('tags: retirement');
    expect(result).toContain('citation_refs: direction:dir-xyz, task:memo-src');
    expect(result).toContain('success_evidence: Evidence here.');
    expect(result).toContain('retirement_condition: Condition here.');
    expect(result).toContain('final_metrics: final_prs_merged=12, final_cycles_active=4');
    expect(result).toContain('rationale: Retired after acceptance criteria were met.');
    expect(result).toContain('reasoning: Direction: full scope shipped.');
    expect(result).toContain('Full enriched memo.');
  });

  it('skips malformed entries (non-object) gracefully', () => {
    const result = buildMemoCitationsSection([
      { task_id: 'memo-valid', summary_excerpt: 'Valid.' },
      'not-an-object',
      null,
      { task_id: 'memo-also-valid', summary_excerpt: 'Also valid.' },
    ]);
    expect(result).toContain('M0 (task_id: memo-valid): Valid.');
    expect(result).toContain('M3 (task_id: memo-also-valid): Also valid.');
  });
});

describe('resolveMemoHandles', () => {
  it('returns empty array when no memos', () => {
    const result = resolveMemoHandles('analysis text', []);
    expect(result).toEqual([]);
  });

  it('returns empty array when analysis text contains no memo handles', () => {
    const memos = [
      { task_id: 'memo-aaa', summary_excerpt: 'Finding A' },
      { task_id: 'memo-bbb', summary_excerpt: 'Finding B' },
    ];
    const analysisText = 'We discovered several issues but did not cite any memos.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual([]);
  });

  it('resolves memo handle M0 to first memo task_id', () => {
    const memos = [
      { task_id: 'memo-first', summary_excerpt: 'First' },
      { task_id: 'memo-second', summary_excerpt: 'Second' },
    ];
    const analysisText = 'This finding supports memo M0.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-first']);
  });

  it('resolves multiple memo handles in order of appearance', () => {
    const memos = [
      { task_id: 'memo-aaa' },
      { task_id: 'memo-bbb' },
      { task_id: 'memo-ccc' },
    ];
    const analysisText = 'Consider memo M0 and memo M2 together.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-aaa', 'memo-ccc']);
  });

  it('handles handles with parentheses like (M0)', () => {
    const memos = [
      { task_id: 'memo-test' },
    ];
    const analysisText = 'As noted in (M0), this pattern emerges.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-test']);
  });

  it('deduplicates repeated handle references', () => {
    const memos = [
      { task_id: 'memo-aaa' },
      { task_id: 'memo-bbb' },
    ];
    const analysisText = 'We cite M0 several times. See memo M0 again.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-aaa']);
  });

  it('skips memos with missing or invalid task_id', () => {
    const memos = [
      { task_id: 'memo-aaa' },
      { summary_excerpt: 'Missing task_id' },
      { task_id: '' },
      { task_id: 'memo-ddd' },
    ] as Array<Record<string, unknown>>;
    const analysisText = 'We reference M0 and M3.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-aaa', 'memo-ddd']);
  });

  it('uses word boundary matching to avoid partial matches', () => {
    const memos = [
      { task_id: 'memo-aaa' },
      { task_id: 'memo-bbb' },
      { task_id: 'memo-ccc' },
    ];
    // "M01" must not match handle "M0"; only the standalone "M1" resolves.
    const analysisText = 'Found in M01 but the real cite is M1.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-bbb']);
  });

  it('handles non-object memo entries gracefully', () => {
    const memos = [
      { task_id: 'memo-aaa' },
      { summary_excerpt: 'entry without task_id' },
      { task_id: 'memo-bbb' },
    ] as Array<Record<string, unknown>>;
    const analysisText = 'Consider memo M0 and memo M2.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['memo-aaa', 'memo-bbb']);
  });

  it('returns task_ids in order of indices found in analysis', () => {
    const memos = [
      { task_id: 'first' },
      { task_id: 'second' },
      { task_id: 'third' },
      { task_id: 'fourth' },
    ];
    // Reverse order in text
    const analysisText = 'memo M3 then memo M1 then memo M0.';
    const result = resolveMemoHandles(analysisText, memos);
    expect(result).toEqual(['fourth', 'second', 'first']);
  });
});

describe('buildEfficiencySnapshotSection', () => {
  it('returns data-unavailable marker when called with null', () => {
    const result = buildEfficiencySnapshotSection(null);
    expect(result).toContain('Pipeline Efficiency Axes');
    expect(result).toContain('data unavailable');
  });

  it('returns data-unavailable marker when axes is empty', () => {
    const result = buildEfficiencySnapshotSection({ axes: {} });
    expect(result).toContain('data unavailable');
  });

  it('renders axis table with current, baseline, and drift when data is present', () => {
    const snapshot = {
      axes: {
        cost_per_merged_pr: { current: 1.5, baseline: 2.0, drift_pct: -25.0 },
        median_api_calls_per_task: { current: 40, baseline: 50, drift_pct: -20.0 },
      },
      baseline_captured_at: '2026-04-05T00:00:00',
      last_snapshot_at: '2026-05-01T00:00:00',
    };
    const result = buildEfficiencySnapshotSection(snapshot);
    expect(result).toContain('Pipeline Efficiency Axes');
    expect(result).toContain('Cost/merged PR');
    expect(result).toContain('1.5');
    expect(result).toContain('| 2 |');
    expect(result).toContain('-25.0%');
    expect(result).toContain('2026-04-05');
    expect(result).toContain('2026-05-01');
  });

  it('formats null current/baseline as n/a and null drift_pct as n/a', () => {
    const snapshot = {
      axes: {
        vm_reuse_rate: { current: null, baseline: null, drift_pct: null },
      },
      baseline_captured_at: null,
      last_snapshot_at: null,
    };
    const result = buildEfficiencySnapshotSection(snapshot);
    expect(result).toContain('VM reuse rate');
    expect(result).toMatch(/n\/a.*n\/a/);
  });

  it('renders positive drift with + sign', () => {
    const snapshot = {
      axes: {
        vm_cold_start_p50_ms: { current: 600, baseline: 500, drift_pct: 20.0 },
      },
    };
    const result = buildEfficiencySnapshotSection(snapshot);
    expect(result).toContain('+20.0%');
  });

  it('renders the injected fleet_utilization axis (string current) with its interpretation note', () => {
    const snapshot = {
      axes: {
        fleet_utilization: {
          current: 'now 6/15 (40%), 24h-avg 38%',
          baseline: null,
          drift_pct: null,
        },
      },
    };
    const result = buildEfficiencySnapshotSection(snapshot);
    expect(result).toContain('Fleet slot utilization');
    expect(result).toContain('now 6/15 (40%), 24h-avg 38%');
    // Interpretation note is appended only when the fleet axis is present.
    expect(result).toContain('throughput is being left on the table');
    expect(result).toContain('distinguish idle headroom from cooled accounts');
  });

  it('omits the fleet interpretation note when no fleet_utilization axis is present', () => {
    const snapshot = {
      axes: { cost_per_merged_pr: { current: 1.5, baseline: 2.0, drift_pct: -25.0 } },
    };
    const result = buildEfficiencySnapshotSection(snapshot);
    expect(result).not.toContain('throughput is being left on the table');
  });
});

describe('Strategic executor set-retirement-critiques verification', () => {
  // Helper to detect if set-retirement-critiques was called (matches the patterns in research-plan.ts)
  const wasRetirementCritiquesSet = (output: string): boolean => {
    return /"op"\s*:\s*"set_retirement_critiques"/.test(output)
      || /\.\/strategic\s+set-retirement-critiques\b/.test(output)
      || /Retirement critiques recorded/i.test(output);
  };

  it('detects JSON op form of set-retirement-critiques call', () => {
    const output = 'Response: {"op":"set_retirement_critiques","status":"ok"}';
    expect(wasRetirementCritiquesSet(output)).toBe(true);
  });

  it('detects CLI invocation form of set-retirement-critiques call', () => {
    const output = 'Now running: ./strategic set-retirement-critiques \'[...]\'';
    expect(wasRetirementCritiquesSet(output)).toBe(true);
  });

  it('detects success message form of set-retirement-critiques call', () => {
    const output = 'Retirement critiques recorded successfully (3 critiques processed).';
    expect(wasRetirementCritiquesSet(output)).toBe(true);
  });

  it('detects success message form case-insensitively', () => {
    const output = 'RETIREMENT CRITIQUES RECORDED at 2026-05-05T10:00:00Z';
    expect(wasRetirementCritiquesSet(output)).toBe(true);
  });

  it('returns false when set-retirement-critiques is not present in output', () => {
    const output = 'Some other output without the required call.';
    expect(wasRetirementCritiquesSet(output)).toBe(false);
  });

  it('returns false for partial matches that do not indicate the call was made', () => {
    const output = 'Consider using set-retirement-critiques in your next analysis.';
    expect(wasRetirementCritiquesSet(output)).toBe(false);
  });

  it('detects set-retirement-critiques with both JSON payload forms in mixed output', () => {
    const outputWithArray = 'Response: {"op":"set_retirement_critiques","payload":[{"direction_id":"d-1"}]}';
    const outputWithSentinel = 'Response: {"op":"set_retirement_critiques","payload":{"no_eligible_retirements":true}}';
    expect(wasRetirementCritiquesSet(outputWithArray)).toBe(true);
    expect(wasRetirementCritiquesSet(outputWithSentinel)).toBe(true);
  });
});

// ── handlePlan / handleResearch task-specific prompt fingerprints ─────────────
// Pins characteristic phrases in each handler's assembled prompt so a future
// edit that drops a load-bearing instruction causes a test failure before merge.
describe('handlePlan / handleResearch task-specific prompt fingerprints', () => {
  const FP_TASK_ID = 'fp-task-001';
  const FP_GH_TOKEN = 'gh-token';
  const FP_OWNER = 'owner';
  const FP_REPO = 'repo';
  const FP_WS = null;
  const FP_STATUS_REF = () => ({ value: '' });
  const FP_TASK = { title: 'Test', description: 'test task', context: {}, project_context: {} };
  const FP_PAYLOAD = { base_branch: 'main', prompt: 'analyze the codebase' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  // ── design-doc helper provisioning (prompt contract honesty) ─────────────────
  // The plan/research prompt advertises `./design-doc get <key>`; the workspace
  // must actually provision that helper (read-only) so the contract is honest.
  it('handlePlan provisions the read-only ./design-doc helper it advertises', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    expectCalledWith(writeDesignDocHelper, FP_TASK_ID, expect.anything(), { readOnly: true });
  });

  it('handleResearch provisions the read-only ./design-doc helper it advertises', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    expectCalledWith(writeDesignDocHelper, FP_TASK_ID, expect.anything(), { readOnly: true });
  });

  // ── handlePlan fingerprints ──────────────────────────────────────────────────

  it('handlePlan prompt contains depends_on_indices (plan output schema cue)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('depends_on_indices');
  });

  it('handlePlan prompt instructs cross-file BUILD-ON ordering (consumer after producer)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('BUILD-ON ordering');
    expect(p).toContain('DIFFERENT files');
  });

  it('handlePlan prompt contains "subtasks" (plan JSON output schema root key)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('"subtasks"');
  });

  it('handlePlan prompt contains related_files (subtask schema field)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('related_files');
  });

  it('handlePlan prompt contains effort field (subtask schema field)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('"effort"');
  });

  it('handlePlan prompt contains "type" field (controls subtask execution mode)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('"type"');
  });

  // ── handlePlan compute_experiment empirical-prerequisite documentation ────────
  // The plan prompt must advertise the new measurement-prerequisite subtype so a
  // plan can express "measure this first, then act on the verdict": the
  // type="compute_experiment" subtask, its launch-contract fields, and the
  // measured-negative-vs-broken consumption semantics that decide whether a
  // depends_on dependent still runs.
  it('handlePlan prompt documents the compute_experiment prerequisite subtype', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('type="compute_experiment"');
  });

  it('handlePlan prompt lists the compute_experiment launch-contract fields', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('"command"');
    expect(p).toContain('"spend_cap_usd"');
    expect(p).toContain('"deadline_seconds"');
    expect(p).toContain('"success_criterion"');
  });

  it('handlePlan prompt explains measured-negative-is-consumable vs broken-cancels semantics', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // A measured negative is still usable evidence — the dependent still runs.
    expect(p).toMatch(/MEASURED NEGATIVE/);
    // A broken run with no usable verdict cancels the dependent follow-on.
    expect(p).toMatch(/BROKEN run/);
    // The verdict is consumed via depends_on_indices.
    expect(p).toContain('depends_on_indices');
  });

  // ── handlePlan atomic schema-change rule (no split model/migrate subtasks) ────
  // Regression: the plan prompt previously told the agent that any subtask
  // touching backend/app/models.py (or adding tables/columns) must spawn a
  // SEPARATE dependent type="migrate" subtask. That manufactured dead
  // serialization and shipped model PRs with no migration. The corrected
  // guidance keeps the schema edit, its Alembic migration, and the revision
  // bump in ONE atomic subtask/PR.
  it('handlePlan prompt no longer instructs a separate dependent migrate subtask for model changes', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The old split-the-migration instruction must be gone.
    expect(p).not.toContain('include a separate subtask with set type="migrate"');
    expect(p).not.toContain('This subtask should depend on the model change subtask');
  });

  it('handlePlan prompt states the atomic schema-change rule (model + migration + revision bump in one subtask)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('ATOMIC SCHEMA RULE');
    // The atomic subtask owns the model file, the Alembic migration, and the
    // EXPECTED_SCHEMA_REVISION bump together.
    expect(p).toContain('backend/alembic/versions/');
    expect(p).toContain('EXPECTED_SCHEMA_REVISION');
    // And it explicitly forbids splitting the migration into a dependent subtask.
    expect(p).toContain('Do NOT emit a separate dependent type="migrate" subtask');
  });

  // ── handlePlan affirmative no-op (#7021 principle one layer up) ───────────────
  // The plan prompt must instruct the agent to emit an affirmative no_op_reason
  // sentinel when there is genuinely nothing to decompose, NOT a silent empty
  // plan. The backend (_handle_plan_completion) lands such a plan CANCELLED;
  // an empty plan WITHOUT the reason still FAILS. These pins fail loudly if a
  // future edit drops the no-op guidance and reopens the no-op→FAILED bug.

  it('handlePlan prompt instructs emitting no_op_reason for verified nothing-to-decompose', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('no_op_reason');
  });

  it('handlePlan prompt states the >=30-char floor and "already done" semantics for the no-op', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // ≥30-char floor must be communicated so the affirmation can't be trivially auto-filled.
    expect(p).toContain('30 char');
    // Must frame the no-op as "already done", not a give-up path.
    expect(p).toMatch(/ALREADY DONE|already done/);
    expect(p).toContain('NOT a give-up');
  });

  it('handlePlan prompt warns against silent empty plans / filler subtasks', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('silent empty plan');
    expect(p).toContain('filler subtasks');
  });

  // ── handleResearch fingerprints ──────────────────────────────────────────────

  it('handleResearch prompt contains "Explore aggressively" (read-only exploration directive)', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('Explore aggressively');
  });

  it('handleResearch prompt contains /tmp/research_findings_ output file path', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('/tmp/research_findings_');
  });

  it('handleResearch prompt contains actionability_grade (output schema required field)', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('actionability_grade');
  });

  it('handleResearch prompt contains verification_basis schema field, all four enum values, and the orthogonality note', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('verification_basis');
    expect(p).toContain('code_verified');
    expect(p).toContain('path_verified');
    expect(p).toContain('pattern_inferred');
    expect(p).toContain('deduced');
    // Orthogonal to actionability_grade and optional with a pattern_inferred default.
    expect(p).toContain('ORTHOGONAL');
    expect(p).toContain('defaults to pattern_inferred');
  });

  it('handleResearch prompt contains /tmp/research_progress_ second output file', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('/tmp/research_progress_');
  });

  it('handleResearch prompt contains related_files (finding output schema field)', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('related_files');
  });

  it('handleResearch prompt teaches the web_research and design knowledge finding types with a source-of-truth rubric', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('Knowledge findings');
    expect(p).toContain('web_research');
    expect(p).toContain('design —');
    // Rubric must distinguish the two by source of truth (outside vs inside the repo).
    expect(p).toContain('OUTSIDE this');
    expect(p).toContain('FILE-GROUNDED design');
    // Liberal-usage framing (operator steer 2026-06-09): knowledge tasks are
    // high-ROI and the rubric must encourage emitting them, not gate them.
    expect(p).toContain('HIGH-ROI');
    expect(p).toContain('err toward emitting');
    // Pairing contract: knowledge findings gate the implementation finding via
    // depends_on_indices instead of replacing it.
    expect(p).toContain('does NOT replace the implementation finding');
  });

  it('handleResearch prompt teaches the compute_experiment finding type with a when-to-use rubric and launch contract', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The finding type and its aliases must be advertised.
    expect(p).toContain('compute_experiment');
    expect(p).toContain('measurement');
    expect(p).toContain('benchmark');
    // When-to-use rubric: a measurable/empirical question that needs running code.
    expect(p).toContain('MEASURABLE');
    expect(p).toContain('RUNNING CODE');
    // Launch contract fields the backend parser reads.
    expect(p).toContain('command');
    expect(p).toContain('spend_cap_usd');
    expect(p).toContain('deadline_seconds');
    // Optional machine-checkable success criterion.
    expect(p).toContain('success_criterion');
  });

  // ── shared capability vocabulary (research + plan, uniform) ──────────────────
  // Both research and plan must render the AVAILABLE CAPABILITIES vocabulary from
  // the new payload field and document provides_capability / prerequisite as
  // drawn from the registry — a capability framework ALL task types share.
  const VOCAB_PAYLOAD = {
    base_branch: 'main',
    prompt: 'analyze the codebase',
    available_capabilities: [
      { name: 'seeded-eval-harness', status: 'working' },
      { name: 'payment-core', status: 'planned' },
    ],
  };

  it('handlePlan renders the AVAILABLE CAPABILITIES vocabulary from the payload field', async () => {
    await handlePlan(FP_TASK_ID, VOCAB_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('AVAILABLE CAPABILITIES');
    expect(p).toContain('`seeded-eval-harness`');
    expect(p).toContain('shared vocabulary');
  });

  it('handlePlan documents provides_capability AND prerequisite drawn from the registry on plan subtasks', async () => {
    await handlePlan(FP_TASK_ID, VOCAB_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The plan subtask schema now carries both fields (today it emitted only depends_on_indices).
    expect(p).toContain('"provides_capability"');
    expect(p).toContain('"prerequisite"');
    // And documents drawing the name from the vocabulary, plan-side (not research-only).
    expect(p).toContain('DRAW THE NAME from the AVAILABLE CAPABILITIES');
    expect(p).toContain('every other task type');
  });

  it('handleResearch renders the AVAILABLE CAPABILITIES vocabulary and draws finding declarations from it', async () => {
    await handleResearch(FP_TASK_ID, VOCAB_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('AVAILABLE CAPABILITIES');
    expect(p).toContain('`payment-core`');
    // provides_capability / prerequisite docs reference the vocabulary.
    expect(p).toContain('provides_capability');
    expect(p).toContain('DRAW THE NAME from the AVAILABLE CAPABILITIES');
  });

  it('omits the rendered vocabulary section when the payload has no capabilities (back-compat)', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    // The rendered list section (with its "shared vocabulary" header framing) is
    // absent — the field-doc rules may still REFERENCE the vocabulary, but no
    // capability list is injected when none were supplied.
    expect(p).not.toContain('# AVAILABLE CAPABILITIES (shared vocabulary)');
  });
});

// ── handleResearch — prior-knowledge injection ───────────────────────────────
// Research must read accumulated knowledge documents back at launch (the same
// way web-research does) and inject the most relevant ones as a Prior Knowledge
// section, fail-soft when none exist or the query errors.
describe('handleResearch — prior-knowledge injection', () => {
  const PK_TASK_ID = 'pk-task-001';
  const PK_GH_TOKEN = 'gh-token';
  const PK_OWNER = 'owner';
  const PK_REPO = 'repo';
  const PK_WS = null;
  const PK_STATUS_REF = () => ({ value: '' });
  const PK_TASK = { title: 'Test', description: 'test task', context: {}, project_context: {} };
  const PK_PAYLOAD = { base_branch: 'main', prompt: 'analyze the caching layer' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  it('injects a Prior Knowledge section with prior-doc ids/titles when docs exist', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      documents: [
        { id: 'doc-aaa', title: 'Earlier caching study', summary: 'A prior look at caching.' },
        { id: 'doc-bbb', title: 'Cache invalidation', summary: 'How invalidation works.' },
      ],
    });

    await handleResearch(PK_TASK_ID, PK_PAYLOAD, PK_TASK, PK_GH_TOKEN, PK_OWNER, PK_REPO, PK_WS, PK_STATUS_REF());

    // Query uses the research focus (prompt) and hits the agent endpoint.
    const calledPath = vi.mocked(apiGet).mock.calls[0][0];
    expect(calledPath).toContain('/api/agent/knowledge-documents');
    expect(calledPath).toContain(`q=${encodeURIComponent('analyze the caching layer')}`);

    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('## Prior Knowledge');
    expect(p).toContain('doc-aaa');
    expect(p).toContain('Earlier caching study');
    expect(p).toContain('doc-bbb');
    expect(p).toContain('supersede');
  });

  it('omits the Prior Knowledge section when the fetch returns no documents', async () => {
    vi.mocked(apiGet).mockResolvedValue({ documents: [] });

    await handleResearch(PK_TASK_ID, PK_PAYLOAD, PK_TASK, PK_GH_TOKEN, PK_OWNER, PK_REPO, PK_WS, PK_STATUS_REF());

    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).not.toContain('## Prior Knowledge');
  });

  it('does not fail the research run when the knowledge fetch throws (fail-soft)', async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error('knowledge endpoint unreachable'));

    await handleResearch(PK_TASK_ID, PK_PAYLOAD, PK_TASK, PK_GH_TOKEN, PK_OWNER, PK_REPO, PK_WS, PK_STATUS_REF());

    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).not.toContain('## Prior Knowledge');
    // The run still completes and posts a status.
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalled();
  });
});

// ── handleResearch — SYSTEM VERIFICATION rendering ───────────────────────────
// The backend annotates the research payload with per-system code-existence
// verdicts (system_verification). handleResearch must surface claimed-but-absent
// and partial systems prominently so tactical research reasons about what
// actually exists rather than trusting a stale design-doc status.
describe('handleResearch — system verification prompt rendering', () => {
  const SV_TASK_ID = 'sv-task-001';
  const SV_GH_TOKEN = 'gh-token';
  const SV_OWNER = 'owner';
  const SV_REPO = 'repo';
  const SV_WS = null;
  const SV_STATUS_REF = () => ({ value: '' });
  const SV_TASK = { title: 'Test', description: 'test task', context: {}, project_context: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  it('renders a SYSTEM VERIFICATION section flagging claimed-but-absent systems', async () => {
    const payload = {
      base_branch: 'main',
      prompt: 'analyze',
      system_verification: {
        ghost: { status: 'claimed_absent', claimed_status: 'mature', present: [], missing: ['backend/app/ghost.py'] },
      },
    };
    await handleResearch(SV_TASK_ID, payload, SV_TASK, SV_GH_TOKEN, SV_OWNER, SV_REPO, SV_WS, SV_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('# SYSTEM VERIFICATION');
    expect(p).toContain('CLAIMED-BUT-ABSENT');
    expect(p).toContain('backend/app/ghost.py');
    expect(p).toContain('ghost');
  });

  it('renders partial systems under the partial heading', async () => {
    const payload = {
      base_branch: 'main',
      prompt: 'analyze',
      system_verification: {
        half: { status: 'partial', claimed_status: 'functional', present: ['backend/app/real.py'], missing: ['backend/app/fake.py'] },
      },
    };
    await handleResearch(SV_TASK_ID, payload, SV_TASK, SV_GH_TOKEN, SV_OWNER, SV_REPO, SV_WS, SV_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('PARTIAL');
    expect(p).toContain('backend/app/fake.py');
  });

  it('omits the SYSTEM VERIFICATION section when no verification is present', async () => {
    const payload = { base_branch: 'main', prompt: 'analyze' };
    await handleResearch(SV_TASK_ID, payload, SV_TASK, SV_GH_TOKEN, SV_OWNER, SV_REPO, SV_WS, SV_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).not.toContain('# SYSTEM VERIFICATION');
  });

  it('surfaces the current direction verdict inside the architecture-context highlight', async () => {
    const payload = {
      base_branch: 'main',
      prompt: 'analyze',
      direction_design_context: {
        system_id: 'sys-x',
        verification: { status: 'claimed_absent', present: [], missing: ['backend/app/x.py'] },
      },
    };
    await handleResearch(SV_TASK_ID, payload, SV_TASK, SV_GH_TOKEN, SV_OWNER, SV_REPO, SV_WS, SV_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('CODE VERIFICATION');
    expect(p).toContain('CLAIMED-BUT-ABSENT');
    expect(p).toContain('backend/app/x.py');
  });
});

// ── Strategic executor prompt — step 1 / step 5 consistency (R4-LOW) ──────────
//
// R4-LOW: the executor prompt's step 1 ("read-and-pass `$(cat ...problem_model...)`,
// don't summarize or re-emit") conflicted with step 5 ("embed
// version_phase_decision / qualify_request as a top-level sibling in the
// problem_model JSON before ./strategic set-problem-model"). An executor
// following step 1 literally would never inject the siblings → a phase flip /
// qualify_request silently dropped. The fix makes the two steps consistent:
// step 5 edits /tmp/problem_model_<taskId>.json IN PLACE and step 1 explicitly
// does step 5 first, then `cat`s the (now-edited) file.
describe('strategic executor prompt — step 1 / step 5 sibling-field consistency', () => {
  const EX_TASK_ID = 'exec-task-001';
  const EX_GH_TOKEN = 'gh-token';
  const EX_OWNER = 'owner';
  const EX_REPO = 'repo';
  const EX_WS = null;
  const EX_STATUS_REF = () => ({ value: '' });
  const EX_TASK = { title: 'Strategic cycle', description: 'strategic', context: {}, project_context: {} };
  // research_mode 'strategic' is the canonical strategic-flow payload shape;
  // handleStrategic owns the thinker→executor two-pass flow where the executor
  // prompt lives.
  const EX_PAYLOAD = { base_branch: 'main', research_mode: 'strategic', prompt: 'run the cycle' };

  /**
   * Drives handleStrategic through the strategic two-pass flow and returns the
   * executor (pass-2) prompt — the THIRD runClaudeCode invocation. The first
   * is the thinker; the second is the adversarial strategic challenge pass;
   * the third is the executor. We make the thinker exit 0 with parseable
   * problem-model JSON (so runThinkerWithGate's repair path is skipped) and
   * report both handoff files present so the gate passes. The executor's
   * allOutput carries the three required ./strategic markers so neither
   * post-check fires a retry — keeping the executor at call index 2.
   */
  async function captureExecutorPrompt(): Promise<string> {
    // Thinker must return parseable JSON with "intent" so repairStructuredOutput
    // in runThinkerWithGate is NOT triggered (an extra runClaudeCode call would
    // shift the challenge and executor to unexpected indices).
    const THINKER_JSON = '{"intent":{"purpose":"thinker","current_focus":"test"}}';
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: THINKER_JSON, lastResultText: THINKER_JSON })
      // Strategic challenge pass (adversarial second opinion) — empty output gives
      // an invalid/blocked record, which is fine; the executor still runs.
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput:
          './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\n',
        lastResultText: '',
      });
    // runThinkerWithGate's gate: thinker exit 0 + both handoff files present.
    // Returning true for every existsSync keeps the gate happy.
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(true);

    await handleStrategic(
      EX_TASK_ID, EX_PAYLOAD, EX_TASK, EX_GH_TOKEN, EX_OWNER, EX_REPO, EX_WS, EX_STATUS_REF(),
    );

    const calls = vi.mocked(runClaudeCode).mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(3);
    return calls[2][0];
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  it('executor prompt has both step 1 (problem model) and step 5 (sibling fields)', async () => {
    const p = await captureExecutorPrompt();
    expect(p).toContain('set-problem-model');
    expect(p).toContain('version_phase_decision');
    expect(p).toContain('qualify_request');
  });

  it('step 5 instructs editing /tmp/problem_model_<taskId>.json IN PLACE (not a separate inline emit)', async () => {
    // The in-place edit is what carries the siblings through step 1's `cat`.
    const p = await captureExecutorPrompt();
    expect(p).toContain(`/tmp/problem_model_${EX_TASK_ID}.json`);
    // Both sibling fields are added by editing the file in place.
    expect(p).toMatch(/edit\s+`?\/tmp\/problem_model_[^`]+\.json`?\s+IN PLACE/i);
  });

  it('step 1 references step 5 so an executor does not call set-problem-model before applying siblings', async () => {
    // The bug was step 1 saying "read-and-pass" with no mention of step 5 — an
    // executor following step 1 literally fired set-problem-model immediately.
    // Step 1 must now tell the executor to do step 5 first.
    const p = await captureExecutorPrompt();
    const step1Idx = p.indexOf('1. **Problem model');
    expect(step1Idx).toBeGreaterThanOrEqual(0);
    // Slice out step 1's text (up to step 2) and assert it forward-references step 5.
    const step2Idx = p.indexOf('2. **Running update');
    expect(step2Idx).toBeGreaterThan(step1Idx);
    const step1Text = p.slice(step1Idx, step2Idx);
    expect(step1Text).toMatch(/step 5/i);
    // Step 1 must still describe the read-and-pass mechanism.
    expect(step1Text).toContain('set-problem-model');
    expect(step1Text).toMatch(/read-and-pass/i);
  });

  it('step 1 no longer tells the executor to pass the file content with no caveat about siblings', async () => {
    // Regression guard: step 1 must not present an unconditional "just cat and
    // pass, do not re-emit" instruction that contradicts step 5. It now permits
    // exactly one edit — step 5's sibling fields.
    const p = await captureExecutorPrompt();
    const step1Idx = p.indexOf('1. **Problem model');
    const step2Idx = p.indexOf('2. **Running update');
    const step1Text = p.slice(step1Idx, step2Idx);
    // The caveat phrasing: the only permitted edit is step 5's siblings.
    expect(step1Text).toMatch(/step 5'?s sibling fields/i);
  });

  it('step 5 explains the cat re-read carries the siblings into set-problem-model', async () => {
    const p = await captureExecutorPrompt();
    const step5Idx = p.indexOf('5. **Version phase');
    expect(step5Idx).toBeGreaterThanOrEqual(0);
    const step6Idx = p.indexOf('6. **Self-reflection');
    expect(step6Idx).toBeGreaterThan(step5Idx);
    const step5Text = p.slice(step5Idx, step6Idx);
    // Step 5 cross-references step 1's cat-based set-problem-model call.
    expect(step5Text).toContain('set-problem-model');
    expect(step5Text).toMatch(/re-read|cat /i);
  });

  // ── evidence_progress verdict (OPTIONAL agent refinement) ──────────────────
  it('thinker prompt has Section 3.6 evidence-progress assessment for long-running directions', async () => {
    await captureExecutorPrompt();
    const thinker = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(thinker).toContain('Evidence-progress assessment');
    expect(thinker).toContain('advancing | stalled | met');
    // It must point the executor at the persistence command.
    expect(thinker).toContain('set-evidence-progress');
  });

  it('executor prompt offers the OPTIONAL set-evidence-progress command', async () => {
    const p = await captureExecutorPrompt();
    expect(p).toContain('set-evidence-progress');
    // Must be marked optional, never folded into the REQUIRED required-call gate.
    expect(p).toMatch(/OPTIONAL[\s\S]*set-evidence-progress/i);
  });

  it('strategic helper script maps set-evidence-progress to the set_evidence_progress op', async () => {
    await captureExecutorPrompt();
    const writeCalls = vi.mocked(fs.writeFileSync).mock.calls as unknown as [string, string][];
    const helperCall = writeCalls.find(([path]) => path.endsWith('/strategic'));
    expect(helperCall).toBeDefined();
    const script = helperCall ? helperCall[1] : '';
    expect(script).toContain('set-evidence-progress)');
    expect(script).toContain('_call "set_evidence_progress"');
    // Usage string advertises the new op.
    expect(script).toContain('set-evidence-progress <json-or-file>');
  });
});

// ── handleStrategic — bounded self-repair on a legacy-strategic-JSON format miss ─
//
// The strategic completion path consumes buildStrategicCompletionLogs (a pure
// function in research-plan.ts). When the executor emits the legacy inline
// markers (direction_operations AND (problem_model OR vision)) but the JSON
// fails to extract on a clean exit, the handler now issues exactly ONE bounded
// re-ask before falling through. A command-based run with no inline JSON (no
// warning) is NOT re-asked — preserved by the existing buildStrategicCompletionLogs
// gate tests above.
describe('handleStrategic — bounded self-repair on legacy strategic JSON format miss', () => {
  const SR_TASK_ID = 'sr-strategic-001';
  const SR_TASK = { title: 'Strategic cycle', description: 'strategic', context: {}, project_context: {} };
  const SR_PAYLOAD = { base_branch: 'main', research_mode: 'strategic', prompt: 'run the cycle' };

  // Thinker mock must return parseable problem-model JSON with "intent" so that
  // repairStructuredOutput in runThinkerWithGate is NOT triggered (otherwise it
  // makes an extra runClaudeCode call that shifts the mock sequence).
  const THINKER_OUTPUT = '{"intent":{"purpose":"test","current_focus":"test"}}';

  // Challenge mock: empty output → invalid/blocked record, gate fails closed.
  // The executor still runs; nothing is pre-approved. Inserting this mock is
  // required because handleStrategic now runs runStrategicChallenge (adversarial
  // second opinion) between the thinker and executor passes.
  const CHALLENGE_MOCK = { exitCode: 0, allOutput: '', lastResultText: '' };

  // Executor output that satisfies the three required-call post-checks AND carries
  // the legacy inline markers in prose (so buildStrategicCompletionLogs sets a
  // warning) but contains no parseable JSON object.
  const EXECUTOR_MARKERS_NO_JSON =
    './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\n' +
    'I prepared the "problem_model" and "direction_operations" but never emitted the JSON object.';
  const REPAIRED_STRATEGIC_JSON = '{"problem_model":{"revision":1},"direction_operations":[]}';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.readFileSync).mockReturnValue('');
    // Thinker gate needs both handoff files present; returning true everywhere
    // also makes the legacy /tmp/strategic_output read attempt fire (it then
    // yields null via the readHandlerOutput mock, so the completion-logs path runs).
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(true);
  });

  function lastPostedStatus(): Record<string, unknown> {
    const calls = vi.mocked(persistAndPostStatus).mock.calls;
    return calls[calls.length - 1][1];
  }

  it('re-asks exactly once and COMPLETES with the recovered inline strategic JSON', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: THINKER_OUTPUT, lastResultText: THINKER_OUTPUT })
      .mockResolvedValueOnce(CHALLENGE_MOCK)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: EXECUTOR_MARKERS_NO_JSON, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: REPAIRED_STRATEGIC_JSON, lastResultText: REPAIRED_STRATEGIC_JSON });

    await handleStrategic(
      SR_TASK_ID, SR_PAYLOAD, SR_TASK, 'gh-token', 'owner', 'repo', null, { value: '' },
    );

    // thinker + challenge + executor + exactly one bounded re-ask = 4 invocations.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(4);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);

    const posted = lastPostedStatus();
    expect(posted.status).toBe('COMPLETED');
    const parsed = JSON.parse(posted.logs as string) as { problem_model: { revision: number } };
    expect(parsed.problem_model.revision).toBe(1);
  });

  it('does NOT record repair success when the re-ask still yields no inline JSON', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: THINKER_OUTPUT, lastResultText: THINKER_OUTPUT })
      .mockResolvedValueOnce(CHALLENGE_MOCK)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: EXECUTOR_MARKERS_NO_JSON, lastResultText: 'fallback summary' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'still no parseable json here', lastResultText: '' });

    await handleStrategic(
      SR_TASK_ID, SR_PAYLOAD, SR_TASK, 'gh-token', 'owner', 'repo', null, { value: '' },
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(4);
    expect(vi.mocked(recordFormatMiss)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairAttempt)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recordFormatRepairSuccess)).not.toHaveBeenCalled();
    // Still COMPLETED (clean exit) with the fallback logs.
    expect(lastPostedStatus().status).toBe('COMPLETED');
  });

  it('counts repair success when the retry delivers JSON only in lastResultText (JSON-stream provider)', async () => {
    // Simulate a JSON-stream provider: allOutput is the raw stream with escaped JSON,
    // lastResultText is the clean final answer with the parseable JSON.
    const STREAM_RAW = './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\n' +
      '{"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"problem_model\\":{\\"revision\\":2},\\"direction_operations\\":[]}"}}';
    // allOutput has the keys only as escaped substrings; lastResultText has the literal keys.
    const CLEAN_JSON = '{"problem_model":{"revision":2},"direction_operations":[]}';

    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: THINKER_OUTPUT, lastResultText: THINKER_OUTPUT })
      .mockResolvedValueOnce(CHALLENGE_MOCK)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: EXECUTOR_MARKERS_NO_JSON, lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: STREAM_RAW, lastResultText: CLEAN_JSON });

    await handleStrategic(
      SR_TASK_ID, SR_PAYLOAD, SR_TASK, 'gh-token', 'owner', 'repo', null, { value: '' },
    );

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(4);
    expect(vi.mocked(recordFormatRepairSuccess)).toHaveBeenCalledTimes(1);
    const posted = lastPostedStatus();
    expect(posted.status).toBe('COMPLETED');
    const parsed = JSON.parse(posted.logs as string) as { problem_model: { revision: number } };
    expect(parsed.problem_model.revision).toBe(2);
  });

  it('does NOT re-ask a command-based run that emits no inline strategic JSON (no warning gate)', async () => {
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: THINKER_OUTPUT, lastResultText: THINKER_OUTPUT })
      .mockResolvedValueOnce(CHALLENGE_MOCK)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: './strategic set-problem-model\n./strategic set-running-update\n./strategic set-retirement-critiques\nProblem model set at revision 40.',
        lastResultText: 'Commands ran successfully.',
      });

    await handleStrategic(
      SR_TASK_ID, SR_PAYLOAD, SR_TASK, 'gh-token', 'owner', 'repo', null, { value: '' },
    );

    // Only thinker + challenge + executor — no re-ask, no format telemetry.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(3);
    expect(vi.mocked(recordFormatMiss)).not.toHaveBeenCalled();
    expect(vi.mocked(recordFormatRepairAttempt)).not.toHaveBeenCalled();
    expect(lastPostedStatus().status).toBe('COMPLETED');
  });
});

// ── handlePlan greenfield bootstrap-from-goal mode ───────────────────────────
// On an empty / near-empty repo the plan prompt must gain an explicit greenfield
// branch that decomposes "build the foundation for the goal" and overrides the
// no-op path (absent source is NOT a valid no_op_reason). A populated repo must
// be unaffected — no greenfield branch, standard no-op guidance intact.
describe('handlePlan greenfield bootstrap mode', () => {
  const GF_TASK_ID = 'gf-task-001';
  const GF_GH_TOKEN = 'gh-token';
  const GF_OWNER = 'owner';
  const GF_REPO = 'repo';
  const GF_WS = null;
  const GF_STATUS_REF = () => ({ value: '' });
  const GF_TASK = { title: 'Test', description: 'test task', context: {}, project_context: {} };
  const GF_PAYLOAD = { base_branch: 'main', prompt: 'build the thing' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  afterEach(() => {
    // Restore the module-default so other suites see an empty tree but
    // verifyCloneDelivered still resolves a HEAD (rev-parse → stable sha).
    vi.mocked(execFileSync).mockImplementation((_file: string, args?: unknown) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (a[0] === 'rev-parse') return 'deadbeefcafe0000' as never;
      return Buffer.from('') as never;
    });
  });

  it('injects the greenfield foundation branch when the tree is config-only', async () => {
    // git ls-files returns only config/scaffolding → 0 source files → greenfield.
    vi.mocked(execFileSync).mockReturnValue(
      'package.json\nREADME.md\ntsconfig.json\n.gitignore\npnpm-lock.yaml\n' as never,
    );
    await handlePlan(GF_TASK_ID, GF_PAYLOAD, GF_TASK, GF_GH_TOKEN, GF_OWNER, GF_REPO, GF_WS, GF_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).toContain('GREENFIELD MODE');
    expect(p).toContain('build the foundation for the project goal');
    // No-op path must be overridden: absent source is not a valid no-op here.
    expect(p).toContain('GREENFIELD CAVEAT');
    expect(p).toContain('is NOT a valid no_op_reason');
    // Standard no-op guidance is still present (greenfield branch is additive).
    expect(p).toContain('no_op_reason');
  });

  it('omits the greenfield branch and keeps standard no-op guidance on a populated repo', async () => {
    // git ls-files returns many real source files → well above the threshold.
    const sourceFiles = Array.from({ length: 20 }, (_, i) => `src/module${i}.ts`).join('\n');
    vi.mocked(execFileSync).mockReturnValue(sourceFiles as never);
    await handlePlan(GF_TASK_ID, GF_PAYLOAD, GF_TASK, GF_GH_TOKEN, GF_OWNER, GF_REPO, GF_WS, GF_STATUS_REF());
    const p = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(p).not.toContain('GREENFIELD MODE');
    expect(p).not.toContain('GREENFIELD CAVEAT');
    // Standard no-op guidance is untouched.
    expect(p).toContain('no_op_reason');
    expect(p).toContain('Affirmative no-op');
  });
});

// ── Prompt determinism meta-tests ─────────────────────────────────────────────
describe('handlePlan / handleResearch prompt determinism', () => {
  const FP_TASK_ID = 'det-task-001';
  const FP_GH_TOKEN = 'gh-token';
  const FP_OWNER = 'owner';
  const FP_REPO = 'repo';
  const FP_WS = null;
  const FP_STATUS_REF = () => ({ value: '' });
  const FP_TASK = { title: 'Test', description: 'test task', context: {}, project_context: {} };
  const FP_PAYLOAD = { base_branch: 'main', prompt: 'analyze the codebase' };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('handlePlan produces identical prompts for identical inputs', async () => {
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const prompt1 = vi.mocked(runClaudeCode).mock.calls[0][0];
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await handlePlan(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const prompt2 = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt1).toBe(prompt2);
  });

  it('handleResearch produces identical prompts for identical inputs', async () => {
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const prompt1 = vi.mocked(runClaudeCode).mock.calls[0][0];
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    await handleResearch(FP_TASK_ID, FP_PAYLOAD, FP_TASK, FP_GH_TOKEN, FP_OWNER, FP_REPO, FP_WS, FP_STATUS_REF());
    const prompt2 = vi.mocked(runClaudeCode).mock.calls[0][0];
    expect(prompt1).toBe(prompt2);
  });
});

// ── handlePlan structured-output self-repair ──────────────────────────────────
describe('handlePlan structured-output self-repair', () => {
  const TASK_ID = 'plan-repair-001';
  const GH_TOKEN = 'gh-token';
  const OWNER = 'owner';
  const REPO = 'repo';
  const WS = null;
  const STATUS_REF = () => ({ value: '' });
  const TASK = { title: 'Plan task', description: 'Decompose the feature', context: {}, project_context: {} };
  const PAYLOAD = { base_branch: 'main', prompt: 'decompose the new feature' };

  const VALID_PLAN = JSON.stringify({
    subtasks: [
      { title: 'Add feature', prompt: 'Implement the feature.', type: 'create', scope: 'medium', related_files: ['agent/src/index.ts'], depends_on_indices: [] },
    ],
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(persistAndPostStatus).mockResolvedValue(undefined);
    vi.mocked(getProviderRunner).mockReturnValue({ displayName: 'claude' } as ReturnType<typeof getProviderRunner>);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockReturnValue(false);
    vi.mocked(fs.readFileSync).mockReturnValue('');
  });

  it('triggers repair when plan file absent and subtasks extraction fails on clean exit', async () => {
    // First run: clean exit but no parseable subtasks JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: 'I analyzed the codebase extensively.', lastResultText: '',
    });
    // Repair run: valid plan JSON.
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 0, allOutput: VALID_PLAN, lastResultText: VALID_PLAN,
    });

    await handlePlan(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('COMPLETED');
    const logs = JSON.parse(payload.logs as string) as { subtasks: unknown[] };
    expect(Array.isArray(logs.subtasks)).toBe(true);
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).toHaveBeenCalledTimes(1);
  });

  it('posts COMPLETED even when plan repair fails to recover subtasks', async () => {
    // Both runs return unparseable output on clean exit.
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });

    await handlePlan(TASK_ID, PAYLOAD, TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Still COMPLETED — plan handler is lenient even when repair fails.
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('COMPLETED');
    expect(recordFormatRepairAttempt).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairSuccess).not.toHaveBeenCalled();
  });
});
