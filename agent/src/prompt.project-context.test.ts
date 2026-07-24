import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('./index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    git: vi.fn().mockReturnValue(''),
  };
});

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execFile: vi.fn(),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import { execFileSync } from 'child_process';
import {
  buildRetryContext,
  formatProjectContext,
  configureAgentPermissions,
  discoverRelatedTests,
  extractTestConventions,
  getTaskTypeHints,
  TASK_TYPE_HINTS,
  extractModuleInterfaces,
  discoverAffectedConsumers,
  formatProvenanceWarnings,
  formatInvestigateCitationProvenance,
  formatAcceptanceEvidence,
  formatProjectBlindSpotSummary,
} from './prompt.js';
import { getReadOnlyTaskTypes, getWriteTaskTypes } from './taskCapabilities.js';

describe('formatProjectContext', () => {
  it('returns a Project Context header even with empty context', () => {
    const result = formatProjectContext({});
    expect(result).toContain('## Project Context');
  });

  it('includes Active Tasks section when active_tasks is populated', () => {
    const ctx = {
      active_tasks: [
        {
          task_type: 'create',
          title: 'Implement login',
          status: 'IN_PROGRESS',
          prompt: 'Add OAuth login flow',
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Active Tasks');
    expect(result).toContain('Implement login');
  });

  it('includes Open PRs section when open_prs is populated', () => {
    const ctx = {
      open_prs: ['PR #42: Add dark mode', 'PR #43: Fix typo in README'],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Open PRs');
    expect(result).toContain('PR #42');
  });

  it('includes Sibling Tasks section when sibling_tasks is populated', () => {
    const ctx = {
      sibling_tasks: [
        { status: 'IN_PROGRESS', title: 'Write documentation', related_files: [], active: true },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('Sibling Tasks');
    expect(result).toContain('Write documentation');
  });

  it('renders failure details (failure_class, error excerpt, changed files) for terminal siblings', () => {
    const ctx = {
      sibling_tasks: [
        {
          status: 'FAILED',
          title: 'Fix auth flow',
          active: false,
          failure_class: 'CODE_ERROR',
          error_excerpt: 'test assertions failed in auth_test.py',
          changed_files: ['src/auth.py', 'src/middleware.py'],
          related_files: ['src/auth.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('[FAILED] Fix auth flow');
    expect(result).toContain('[CODE_ERROR: test assertions failed in auth_test.py]');
    expect(result).toContain('(changed: src/auth.py, src/middleware.py)');
    // Should not duplicate the related_files-based files: suffix when changed_files is present
    expect(result).not.toContain('(files: src/auth.py)');
  });

  it('falls back to related_files when terminal sibling has no changed_files', () => {
    const ctx = {
      sibling_tasks: [
        {
          status: 'FAILED',
          title: 'Refactor cache',
          active: false,
          failure_class: 'INFRA',
          error_excerpt: 'provisioning failed',
          related_files: ['src/cache.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('[INFRA: provisioning failed]');
    expect(result).toContain('(files: src/cache.py)');
  });

  it('includes Recently Completed Tasks section when populated', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'create',
          title: 'Completed feature',
          pr_url: 'https://github.com/org/repo/pull/10',
          research_findings_summary: '',
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('### Recently Completed Tasks');
    expect(result).toContain('Completed feature');
  });

  it('includes enriched fields (direction_id, related_files, prompt, changed_files) on completed tasks', () => {
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'create',
          title: 'Succeeded task',
          pr_url: 'https://github.com/org/repo/pull/1',
          direction_id: 'd-123',
          related_files: ['a.py', 'b.py', 'c.py', 'd.py'],
          prompt: 'Add caching to the user lookup endpoint to reduce DB load.',
        },
        {
          task_type: 'create',
          title: 'Failed task',
          failure_reason: 'tests failed',
          failure_class: 'CODE_ERROR',
          changed_files: ['x.py', 'y.py'],
        },
      ],
    };
    const result = formatProjectContext(ctx);
    expect(result).toContain('dir=d-123');
    expect(result).toContain('files: a.py, b.py, c.py +1');
    expect(result).toContain('prompt: Add caching');
    expect(result).toContain('changed: x.py, y.py');
  });

  it('exercises parseResearchSummary via research_findings_summary on completed tasks', () => {
    const findings = JSON.stringify([
      { title: 'Perf finding', description: 'The query is slow because of N+1 joins.' },
      { title: 'Second finding', description: 'Missing index on user_id column.' },
    ]);
    const ctx = {
      recently_completed_tasks: [
        {
          task_type: 'research',
          title: 'Research task',
          research_findings_summary: findings,
        },
      ],
    };
    const result = formatProjectContext(ctx);
    // The summary should be parsed and truncated into the output
    expect(result).toContain('Research task');
    expect(result).toContain('Recently Completed Tasks');
  });

  it('keeps output under ~10 000 chars even with very large context (CONTEXT_BUDGET enforcement)', () => {
    const manyTasks = Array.from({ length: 60 }, (_, i) => ({
      task_type: 'create',
      title: `Task number ${i}`,
      status: 'IN_PROGRESS',
      prompt: `Prompt for task ${i}. `.repeat(10),
    }));
    const ctx = {
      active_tasks: manyTasks.slice(0, 20),
      open_prs: Array.from({ length: 20 }, (_, i) => `PR #${i}: Some long PR title here`),
      sibling_tasks: manyTasks.slice(0, 20).map((t) => ({ ...t, related_files: [] })),
      recently_completed_tasks: manyTasks.slice(0, 20).map((t) => ({ ...t, pr_url: '' })),
    };
    const result = formatProjectContext(ctx);
    expect(result.length).toBeLessThan(10_000);
  });

  it('includes task-type-specific pipeline health stat when by_type is provided', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        first_attempt_success_rate: {
          overall: 0.72,
          sample_size: 50,
          by_type: { create: 0.9 },
        },
        failure_distribution: { CODE_ERROR: 3, RATE_LIMIT: 1 },
        direction_stats: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Pipeline Health');
    expect(result).toContain('Success rate');
    expect(result).toContain('90%');
  });

  it('renders direction_stats with cvr label using conversion_rate (not merge rate)', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [
          {
            direction_id: 'd-abcdef12345678',
            cycles: 5,
            possibly_saturated: false,
            conversion_rate: 0.42,
            proven_prs_merged: 2,
            consumed_prs_merged: 1,
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('cvr 0.42');
    expect(result).toContain('5 cycles');
    expect(result).toContain('proof 2 proven (1 consumed downstream)');
    expect(result).not.toContain('merge rate');
  });

  it('omits zero-only pipeline direction proof split for legacy payloads', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [
          {
            direction_id: 'd-legacy12345678',
            cycles: 2,
            possibly_saturated: false,
            conversion_rate: 0.5,
            proven_prs_merged: 0,
            consumed_prs_merged: 0,
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('cvr 0.5');
    expect(result).not.toContain('0 proven');
    expect(result).not.toContain('consumed downstream');
  });

  it('includes scope-specific pipeline health stat when by_scope is provided', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.85,
        trend: 'stable',
        first_attempt_success_rate: {
          overall: 0.72,
          sample_size: 50,
          by_type: { create: 0.9 },
          by_scope: { medium: 0.75 },
        },
        failure_distribution: {},
        direction_stats: [],
      },
    };
    const result = formatProjectContext(ctx, 'create', 'medium');
    expect(result).toContain('Your scope (medium): 75%');
  });

  it('shows trend indicators in Common Review Issues when review_effectiveness is provided', () => {
    const ctx = {
      review_issue_distribution: {
        'test-coverage': 0.35,
        'error-handling': 0.25,
        'type-safety': 0.15,
      },
      review_effectiveness: {
        categories: {
          'test-coverage': { trend: 'decreasing' },
          'error-handling': { trend: 'stable' },
          'type-safety': { trend: 'increasing' },
        },
        persistent_issues: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('test-coverage');
    expect(result).toContain(', trending down');
    expect(result).toContain(', persistent');
    expect(result).toContain(', trending up');
  });

  it('omits trend indicators when review_effectiveness is absent', () => {
    const ctx = {
      review_issue_distribution: {
        'test-coverage': 0.35,
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('test-coverage');
    expect(result).not.toContain('trending');
    expect(result).not.toContain('persistent');
  });

  it('surfaces persistent review issues in pipeline health', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.80,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [],
        first_attempt_success_rate: { overall: 0.60, sample_size: 15 },
        persistent_review_issues: ['test-coverage', 'error-handling'],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Persistent review issues');
    expect(result).toContain('test-coverage');
    expect(result).toContain('error-handling');
  });

  it('renders SR blind spots block when self_review_catch_rate has qualifying categories', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8 },
          'test-coverage': { miss_rate: 0.2 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('error-handling (80% missed)');
    expect(result).toContain('apply extra scrutiny');
  });

  it('omits SR blind spots block when self_review_catch_rate is absent', () => {
    const ctx = { review_issue_distribution: { 'test-coverage': 0.5 } };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review Blind Spots');
  });

  it('omits SR blind spots block when self_review_catch_rate has empty categories', () => {
    const ctx = { self_review_catch_rate: { categories: {} } };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review Blind Spots');
  });

  it('sorts SR blind spots by miss_rate descending and caps at top-5', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'cat-a': { miss_rate: 0.1 },
          'cat-b': { miss_rate: 0.9 },
          'cat-c': { miss_rate: 0.7 },
          'cat-d': { miss_rate: 0.5 },
          'cat-e': { miss_rate: 0.3 },
          'cat-f': { miss_rate: 0.8 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    // Top-5 by miss_rate: cat-b(90%), cat-f(80%), cat-c(70%), cat-d(50%), cat-e(30%)
    expect(result).toContain('cat-b (90% missed)');
    expect(result).toContain('cat-f (80% missed)');
    expect(result).not.toContain('cat-a');
    // cat-b should appear before cat-c in the output
    const bIdx = result.indexOf('cat-b');
    const cIdx = result.indexOf('cat-c');
    expect(bIdx).toBeLessThan(cIdx);
  });

  it('renders example issues in SR blind spots block when examples present', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.8,
            examples: [
              { severity: 'MAJOR', desc: 'Missing null check in handler' },
              { severity: 'MINOR', desc: 'Unhandled async error' },
            ],
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('[MAJOR] Missing null check in handler');
    expect(result).toContain('[MINOR] Unhandled async error');
  });

  it('falls back to percentage-only line per category when examples absent', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('error-handling');
    expect(result).toContain('80% missed');
    expect(result).not.toContain('[MAJOR]');
    expect(result).not.toContain('[MINOR]');
  });

  it('falls back to percentage-only when examples is empty array', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': { miss_rate: 0.8, examples: [] },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('error-handling');
    expect(result).toContain('80% missed');
    expect(result).not.toContain('[MAJOR]');
  });

  it('still renders category line even when its examples are missing', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'correctness': { miss_rate: 0.9 },
          'error-handling': { miss_rate: 0.7, examples: [{ severity: 'CRITICAL', desc: 'Off-by-one in loop' }] },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    // Both categories should appear
    expect(result).toContain('correctness');
    expect(result).toContain('error-handling');
    // Only the one with examples shows severity tag
    expect(result).toContain('[CRITICAL] Off-by-one in loop');
    // correctness has no examples so no tags for it
    const correctnessIdx = result.indexOf('correctness');
    const criticalIdx = result.indexOf('[CRITICAL]');
    // CRITICAL tag should appear after correctness bullet
    expect(criticalIdx).toBeGreaterThan(correctnessIdx);
  });

  it('shows trajectory hint when previous_hit_rate and current_hit_rate are present', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.3,
            current_hit_rate: 0.7,
            previous_hit_rate: 0.8,
            trend: 'decreasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    // Should show trajectory: 20% → 30% (−10pp regressing)
    expect(result).toContain('last cycle: 20%');
    expect(result).toContain('now: 30%');
    expect(result).toContain('−10pp');
  });

  it('highlights regression when trend is decreasing', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.4,
            current_hit_rate: 0.6,
            previous_hit_rate: 0.7,
            trend: 'decreasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('⚠ Regression: scrutinize extra carefully');
    expect(result).toContain('regressing');
  });

  it('shows improvement when trajectory indicates lower miss rate', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'correctness': {
            miss_rate: 0.2,
            current_hit_rate: 0.8,
            previous_hit_rate: 0.6,
            trend: 'increasing',
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('last cycle: 40%');
    expect(result).toContain('now: 20%');
    expect(result).toContain('+20pp improving');
  });

  it('includes co-change budget note for medium-scope tasks', () => {
    const ctx = { cochange_files: ['src/foo.ts', 'src/bar.ts'] };
    const result = formatProjectContext(ctx, 'create', 'medium', '', 3);
    expect(result).toContain('Frequently Co-Changed Files');
    expect(result).toContain('Budget note: you have 7 production file slots remaining');
  });

  it('omits co-change budget note for non-medium scopes', () => {
    const ctx = { cochange_files: ['src/foo.ts'] };
    const smallResult = formatProjectContext(ctx, 'create', 'small', '', 1);
    expect(smallResult).toContain('Frequently Co-Changed Files');
    expect(smallResult).not.toContain('Budget note');
    const unscoped = formatProjectContext(ctx, 'create');
    expect(unscoped).not.toContain('Budget note');
  });

  it('omits persistent review issues line when list is empty or absent', () => {
    const ctx = {
      pipeline_health: {
        success_rate: 0.80,
        trend: 'stable',
        failure_distribution: {},
        direction_stats: [],
        first_attempt_success_rate: { overall: 0.60, sample_size: 15 },
        persistent_review_issues: [],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Persistent review issues');
  });

  it('renders direction-scoped SR calibration block when self_review_catch_rate_for_direction present', () => {
    const ctx = {
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'Self-review catches issues a skilled human reviewer would',
        sample_count: 5,
        miss_rate: 0.6,
        categories: {
          'regression_risk': {
            miss_rate: 0.7,
            examples: [{ severity: 'MAJOR', desc: 'Mocked migration masked breakage' }],
          },
          'scope-drift': { miss_rate: 0.4 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('For This Direction: SR Calibration');
    expect(result).toContain('Self-review catches issues a skilled human reviewer would');
    expect(result).toContain('5 tasks sampled');
    expect(result).toContain('regression_risk (70% missed)');
    expect(result).toContain('[MAJOR] Mocked migration masked breakage');
    // worst-first ordering
    const regIdx = result.indexOf('regression_risk');
    const drIdx = result.indexOf('scope-drift');
    expect(regIdx).toBeLessThan(drIdx);
  });

  it('omits direction-scoped SR calibration block when self_review_catch_rate_for_direction absent', () => {
    const ctx = {
      self_review_catch_rate: { categories: { 'error-handling': { miss_rate: 0.8 } } },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('For This Direction: SR Calibration');
  });

  it('renders direction-scoped block alongside project-wide Self-Review Blind Spots', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: { 'error-handling': { miss_rate: 0.8 } },
      },
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'Some direction',
        sample_count: 4,
        miss_rate: 0.5,
        categories: { 'regression_risk': { miss_rate: 0.6 } },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Self-Review Blind Spots');
    expect(result).toContain('For This Direction: SR Calibration');
    // The project-wide block should appear before the direction block
    const projectIdx = result.indexOf('Self-Review Blind Spots');
    const dirIdx = result.indexOf('For This Direction: SR Calibration');
    expect(projectIdx).toBeLessThan(dirIdx);
  });

  it('omits direction-scoped block when categories empty', () => {
    const ctx = {
      self_review_catch_rate_for_direction: {
        direction_id: 'd-abc123',
        direction_text: 'x',
        sample_count: 3,
        categories: {},
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('For This Direction: SR Calibration');
  });

  it('renders sr_blind_spot_themes section when themes are provided', () => {
    const ctx = {
      sr_blind_spot_themes: [
        { theme: 'did not verify auth flow', count: 5 },
        { theme: 'untested error handling path', count: 2 },
        { theme: 'race condition in async fetch', count: 1 },
      ],
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Project-wide self-review blind spots (last 14 days');
    expect(result).toContain('did not verify auth flow (5 tasks)');
    expect(result).toContain('untested error handling path (2 tasks)');
    expect(result).toContain('race condition in async fetch (1 task)');
  });

  it('omits sr_blind_spot_themes section when array is empty or absent', () => {
    const empty = formatProjectContext({ sr_blind_spot_themes: [] }, 'create');
    expect(empty).not.toContain('Project-wide self-review blind spots');
    const absent = formatProjectContext({}, 'create');
    expect(absent).not.toContain('Project-wide self-review blind spots');
  });
});

// ── formatProjectBlindSpotSummary ─────────────────────────────────────────────
describe('formatProjectBlindSpotSummary', () => {
  it('returns empty string for empty array', () => {
    expect(formatProjectBlindSpotSummary([])).toBe('');
  });

  it('renders themes with counts and correct singular/plural', () => {
    const result = formatProjectBlindSpotSummary([
      { theme: 'theme one', count: 1 },
      { theme: 'theme two', count: 2 },
    ]);
    expect(result).toContain('- theme one (1 task)');
    expect(result).toContain('- theme two (2 tasks)');
    expect(result).toContain('Project-wide self-review blind spots');
  });

  it('caps at top 8 entries', () => {
    const themes = Array.from({ length: 12 }, (_, i) => ({
      theme: `theme number ${i}`,
      count: 12 - i,
    }));
    const result = formatProjectBlindSpotSummary(themes);
    expect(result).toContain('theme number 0');
    expect(result).toContain('theme number 7');
    expect(result).not.toContain('theme number 8');
    expect(result).not.toContain('theme number 11');
  });
});

// ── configureAgentPermissions ─────────────────────────────────────────────────
describe('configureAgentPermissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(getReadOnlyTaskTypes())(
    'returns [Write, Edit, NotebookEdit] for read-only type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual(['Write', 'Edit', 'NotebookEdit']);
    },
  );

  it.each(getWriteTaskTypes())(
    'returns [] (no restrictions) for write type "%s"',
    (taskType) => {
      const result = configureAgentPermissions(taskType);
      expect(result).toEqual([]);
    },
  );

  it('returns [] for an unknown task type', () => {
    expect(configureAgentPermissions('unknown_type')).toEqual([]);
  });

  it('installs exit-1 pre-commit and pre-push block hooks for a read-only type', () => {
    configureAgentPermissions('research');
    const writes = vi.mocked(fs.writeFileSync).mock.calls;
    const hookPaths = writes.map((c) => c[0]);
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-commit');
    expect(hookPaths).toContain('/tmp/workspace/.git/hooks/pre-push');
    for (const [, body] of writes) {
      expect(typeof body).toBe('string');
      expect(body as string).toContain('exit 1');
      expect(body as string).toContain('read-only analysis task');
    }
  });

  // Regression: a write task reusing a warm VM whose prior role was read-only
  // must clear the leftover block hooks, or it can't commit/push its work.
  it('removes leftover read-only block hooks for a write type on a reused warm VM', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string)
      .mockReturnValue(
        '#!/bin/sh\necho "ERROR: This is a read-only analysis task. Do NOT commit or push." >&2\nexit 1\n',
      );

    const result = configureAgentPermissions('fix');

    expect(result).toEqual([]);
    const unlinked = vi.mocked(fs.unlinkSync).mock.calls.map((c) => c[0]);
    expect(unlinked).toContain('/tmp/workspace/.git/hooks/pre-commit');
    expect(unlinked).toContain('/tmp/workspace/.git/hooks/pre-push');
  });

  it('leaves a non-read-only hook untouched for a write type (marker guard)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string)
      .mockReturnValue('#!/bin/sh\nnpm run lint\n');

    configureAgentPermissions('fix');

    expect(fs.unlinkSync).not.toHaveBeenCalled();
  });

  it('is a no-op for a write type when no leftover hooks exist', () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    configureAgentPermissions('fix');
    expect(fs.unlinkSync).not.toHaveBeenCalled();
  });

});

// ── getTaskTypeHints ──────────────────────────────────────────────────────────
describe('getTaskTypeHints', () => {
  it('returns static hint from TASK_TYPE_HINTS when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['create'];
    expect(result).toBe(TASK_TYPE_HINTS['create']);
    // 2026-05-04: hint now points at ./check / ./pr / ./done helpers (the
    // wrapper-driven lint-loop language was removed in 5c186061).
    expect(result).toContain('./check');
    expect(result).toContain('./pr open');
    expect(result).toContain('./done');
  });

  it('returns undefined for unknown task type when buildConfig is undefined', () => {
    const result = getTaskTypeHints(undefined)['unknown_type'];
    expect(result).toBeUndefined();
  });

  it('fix hint requires reproduce-on-HEAD first with a non-reproducible no-op off-ramp', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    expect(hint).toContain('REPRODUCE FIRST');
    expect(hint).toContain('reproduces on current HEAD');
    expect(hint).toContain('./done --already-done');
  });

  it('fix hint references the structured reproduce verdict and names all three outcomes', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    expect(hint).toContain('already_resolved');
    expect(hint).toContain('reproduce-on-HEAD verdict');
    expect(hint).toContain('indeterminate');
    expect(hint).toContain('./done --reproduced');
    expect(hint).toContain('./done --indeterminate');
  });

  it('fix hint separates code-review fix and defect reproduction into two labeled branches', () => {
    const hint = TASK_TYPE_HINTS['fix'];
    // Two distinct labeled branches
    expect(hint).toContain('Code-review fix');
    expect(hint).toContain('Defect reproduction');
    // Code-review branch (common case) is front-loaded before defect reproduction
    const codeReviewIdx = hint.indexOf('Code-review fix');
    const defectIdx = hint.indexOf('Defect reproduction');
    expect(codeReviewIdx).toBeGreaterThan(-1);
    expect(defectIdx).toBeGreaterThan(-1);
    expect(codeReviewIdx).toBeLessThan(defectIdx);
    // Code-review branch has the end-to-end flow inline (no reproduce-first noise)
    const codeReviewSection = hint.substring(codeReviewIdx, defectIdx);
    expect(codeReviewSection).toContain('./check');
    expect(codeReviewSection).toContain('./pr open');
    expect(codeReviewSection).toContain('./done');
  });

  it('investigate hint asks for the canonical reproduce verdict, not free-text only', () => {
    const hint = TASK_TYPE_HINTS['investigate'];
    expect(hint).toContain('reproduce-on-HEAD verdict');
    expect(hint).toContain('indeterminate');
  });

  it('returns static hint when buildConfig has empty directories array', () => {
    const result = getTaskTypeHints({ directories: [] })['create'];
    expect(result).toBe(TASK_TYPE_HINTS['create']);
  });

  it('generates dynamic hint referencing test commands when buildConfig has directories', () => {
    const result = getTaskTypeHints({
      directories: [
        { path: 'web', type: 'node', lint_cmd: 'npm run lint', build_cmd: 'npm run build', test_cmd: 'npm test' },
      ],
    })['create'];
    expect(result).toContain('npm test');
  });

  it('includes only present commands in dynamic hint (skips missing ones)', () => {
    const result = getTaskTypeHints({
      directories: [
        { path: 'api', type: 'python', test_cmd: 'pytest' },
      ],
    })['create'];
    expect(result).toContain('pytest');
    // No npm commands configured for this python-only directory
    expect(result).not.toContain('npm run lint');
  });

  it('substitutes promotionTarget into release_audit hint when provided', () => {
    const result = getTaskTypeHints(undefined, 'production')['release_audit'];
    expect(result).toContain('(production)');
    expect(result).toContain("production's standards");
    expect(result).not.toContain('(main)');
  });

  it('names a rolling integration promotionTarget (automain) as the promotion branch, not an LTS branch', () => {
    const result = getTaskTypeHints(undefined, 'automain')['release_audit'];
    expect(result).toContain('promotion/integration branch (automain)');
    expect(result).toContain("automain's standards");
    // automain is a rolling integration/rollup branch, not an LTS branch —
    // the dynamic hint must not describe it as one.
    expect(result).not.toContain('LTS branch');
    expect(result).not.toMatch(/LTS/);
  });

  it('substitutes non-main promotionTarget like release/v2 into release_audit hint', () => {
    const result = getTaskTypeHints(undefined, 'release/v2')['release_audit'];
    expect(result).toContain('(release/v2)');
    expect(result).toContain("release/v2's standards");
  });

  it('uses generic LTS / promotion branch wording in release_audit hint when no promotionTarget provided', () => {
    const result = getTaskTypeHints(undefined)['release_audit'];
    expect(result).toContain('LTS / promotion branch');
    expect(result).not.toContain('(main)');
  });

  it('keeps generic LTS / promotion branch wording in release_audit when promotionTarget is absent and buildConfig has dirs', () => {
    const result = getTaskTypeHints({ directories: [{ path: 'src', type: 'node' }] })['release_audit'];
    expect(result).toContain('LTS / promotion branch');
    expect(result).not.toContain('(main)');
  });
});

// ── discoverRelatedTests ──────────────────────────────────────────────────────
describe('discoverRelatedTests', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns empty string when relatedFiles is empty', () => {
    const result = discoverRelatedTests(null, []);
    expect(result).toBe('');
  });

  it('returns empty string when relatedFiles is empty with buildConfig', () => {
    const result = discoverRelatedTests(null, [], {
      directories: [{ path: 'web', type: 'node' }],
    });
    expect(result).toBe('');
  });

  it('falls back to hardcoded discovery when buildConfig is undefined', () => {
    // With no matching test files (fs mocked to not find them), returns empty string
    const result = discoverRelatedTests(null, ['backend/app/main.py'], undefined);
    // The function tries to find test files via fs; since existsSync returns false, it returns ''
    expect(typeof result).toBe('string');
  });

  it('uses buildConfig directory config to discover tests for a matching directory', () => {
    // With buildConfig pointing to a directory, the function looks for tests
    // Since existsSync is mocked to false, no test files are found but no error thrown
    const result = discoverRelatedTests(null, ['myapp/src/utils.ts'], {
      directories: [{ path: 'myapp', type: 'node' }],
    });
    expect(typeof result).toBe('string');
  });

  it('skips files not matching any buildConfig directory', () => {
    const result = discoverRelatedTests(null, ['other/file.ts'], {
      directories: [{ path: 'myapp', type: 'node' }],
    });
    // No directory matches 'other/' so no test entries are added
    expect(result).toBe('');
  });

  it('import-based: issues Python import grep with correct dotted module path', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['backend/app/orchestrator.py'], undefined);
    const calls = mockExecFileSync.mock.calls;
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('app.orchestrator'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: constructs correct Python module path from nested file', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['backend/app/services/auth.py'], undefined);
    const calls = mockExecFileSync.mock.calls;
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('app.services.auth'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: issues TypeScript import grep with stem-based pattern', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['agent/src/prompt.ts'], undefined);
    const calls = mockExecFileSync.mock.calls;
    // The import-based grep pattern should reference the stem 'prompt'
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes('/prompt'))
    );
    expect(importCall).toBeTruthy();
  });

  it('import-based: skips TypeScript test files themselves (contains .test.)', () => {
    const mockExecFileSync = vi.mocked(execFileSync);
    mockExecFileSync.mockReturnValue(Buffer.from(''));
    discoverRelatedTests(null, ['agent/src/prompt.test.ts'], undefined);
    const calls = mockExecFileSync.mock.calls;
    // The import grep uses a pattern with "from ['" — should not appear for test files
    const importCall = calls.find(c =>
      Array.isArray(c[1]) && c[1].some((a: unknown) => typeof a === 'string' && a.includes("from ['"))
    );
    expect(importCall).toBeUndefined();
  });
});

// ── extractTestConventions ────────────────────────────────────────────────────
describe('extractTestConventions', () => {
  let mockFs: { existsSync: ReturnType<typeof vi.fn>; readFileSync: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    vi.clearAllMocks();
    vi.resetModules();
    const fs = await import('fs');
    mockFs = fs.default as unknown as typeof mockFs;
    // Clear the module-level cache between tests by using unique dirKeys
  });

  it('returns empty string when no test files exist', () => {
    mockFs.existsSync.mockReturnValue(false);
    const result = extractTestConventions('backend-empty', ['backend/testing/test_foo.py']);
    expect(result).toBe('');
  });

  it('extracts pytest fixture names from python test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      '@pytest_asyncio.fixture\nasync def db_session():\n    pass\n@pytest.fixture\ndef mock_client():\n    pass\n'
    );
    const result = extractTestConventions('backend-fixtures', ['backend/testing/test_services.py']);
    expect(result).toContain('db_session');
    expect(result).toContain('mock_client');
    expect(result).toContain('Fixtures:');
  });

  it('extracts AsyncMock usage from python test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      'from unittest.mock import AsyncMock, patch\n\nAsyncMock()\nMagicMock()\n'
    );
    const result = extractTestConventions('backend-mocks', ['backend/testing/test_agent.py']);
    expect(result).toContain('AsyncMock');
    expect(result).toContain('Mocks:');
  });

  it('extracts vitest patterns from TypeScript test files', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      'import { vi, describe } from "vitest";\nbeforeEach(() => {});\nvi.fn();\nexpect(x).toBe(1);\n'
    );
    const result = extractTestConventions('agent-vitest', ['agent/src/prompt.test.ts']);
    expect(result).toContain('vitest');
    expect(result).toContain('beforeEach');
  });

  it('returns string under 500 chars', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue(
      '@pytest.fixture\ndef fixture_one(): pass\n@pytest.fixture\ndef fixture_two(): pass\n' +
      'from unittest.mock import AsyncMock\nAsyncMock()\nbeforeEach(() => {})\nassert x == 1\npytest.raises(ValueError)\n'
    );
    const result = extractTestConventions('backend-size', ['backend/testing/test_x.py']);
    expect(result.length).toBeLessThanOrEqual(500);
  });

  it('returns cached result on second call with same dirKey', () => {
    mockFs.existsSync.mockReturnValue(true);
    mockFs.readFileSync.mockReturnValue('@pytest.fixture\ndef session(): pass\n');
    const first = extractTestConventions('backend-cached', ['backend/testing/test_a.py']);
    // Reset mock to return different content — cached result should be returned
    mockFs.readFileSync.mockReturnValue('');
    const second = extractTestConventions('backend-cached', ['backend/testing/test_b.py']);
    expect(first).toBe(second);
  });
});

// ── buildRetryContext ─────────────────────────────────────────────────────────
describe('buildRetryContext', () => {
  it('returns empty string for first-attempt tasks (no _attempt_number)', () => {
    expect(buildRetryContext({})).toBe('');
  });

  it('returns empty string when _attempt_number is 1', () => {
    expect(buildRetryContext({ context: { _attempt_number: 1 } })).toBe('');
  });

  it('renders failure type and error for a basic retry', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'Test suite failed: assertion error on line 42',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 failed)');
    expect(result).toContain('Failure type: CODE_ERROR');
    expect(result).toContain('Test suite failed: assertion error on line 42');
  });

  it('renders PR URL and changed files when present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'lint error',
        _previous_pr_url: 'https://github.com/org/repo/pull/99',
        _previous_changed_files: ['backend/app/models.py', 'backend/testing/test_models.py'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('PR from previous attempt: https://github.com/org/repo/pull/99');
    expect(result).toContain('Files changed in previous attempt:');
    expect(result).toContain('  - backend/app/models.py');
    expect(result).toContain('  - backend/testing/test_models.py');
  });

  it('uses file-specific guidance when changed files are provided', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Review the files listed above');
    expect(result).not.toContain('run the linter before committing');
  });

  it('uses generic guidance when no changed files are provided', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Analyze the error above and avoid the same mistake');
    expect(result).not.toContain('Review the files listed above');
  });

  it('omits PR URL section when _previous_pr_url is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('PR from previous attempt');
  });

  it('ends with a blank line', () => {
    const task = { context: { _attempt_number: 2, _previous_failure_class: 'CODE_ERROR', _previous_error: 'err' } };
    const result = buildRetryContext(task);
    expect(result.endsWith('\n')).toBe(true);
  });

  it('renders failure stage when _previous_failure_stage is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_failure_stage: 'test',
        _previous_error: 'pytest: FAILED',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Failure type: CODE_ERROR (at test stage)');
    expect(result).not.toContain('Failure type: CODE_ERROR\n');
  });

  it('omits stage qualifier when _previous_failure_stage is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Failure type: AGENT_TIMEOUT\n');
    expect(result).not.toContain('at stage)');
  });

  it('renders recurring warning when _previous_is_recurring is true', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_is_recurring: true,
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('recurring failure pattern');
    expect(result).toContain('fundamentally different approach');
  });

  it('does not render recurring warning when _previous_is_recurring is false', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_is_recurring: false,
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('recurring failure pattern');
  });

  it('renders error summary with "Error summary:" label when _previous_error_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'full long error message here',
        _previous_error_summary: 'AssertionError: expected 1 got 2',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Error summary: AssertionError: expected 1 got 2');
    expect(result).not.toContain('Error: full long error message here');
  });

  it('falls back to "Error:" label when only _previous_error is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'AGENT_TIMEOUT',
        _previous_error: 'timed out after 600s',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Error: timed out after 600s');
    expect(result).not.toContain('Error summary:');
  });

  it('renders enriched diff summary with line counts when _previous_diff_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_diff_summary: [
          { file: 'backend/app/models.py', additions: 45, deletions: 12, change_type: 'modified' },
          { file: 'backend/testing/test_models.py', additions: 30, deletions: 0, change_type: 'added' },
        ],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('backend/app/models.py (modified, +45/-12 lines)');
    expect(result).toContain('backend/testing/test_models.py (added, +30/-0 lines)');
    expect(result).toContain('Files changed in previous attempt:');
  });

  it('uses diff summary over plain file list when both are present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
        _previous_diff_summary: [
          { file: 'src/foo.ts', additions: 10, deletions: 5, change_type: 'modified' },
        ],
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('src/foo.ts (modified, +10/-5 lines)');
    // Should not show the plain format alongside the enriched format
    expect(result).not.toMatch(/ {2}- src\/foo\.ts\n/);
  });

  it('renders test failure summary when _previous_test_failure_summary is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test_models.py::test_create FAILED',
        _previous_changed_files: ['src/foo.ts'],
        _previous_test_failure_summary: 'FAILED test_models.py::test_create - AssertionError: expected 1 got 2',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Test failures from previous attempt:');
    expect(result).toContain('FAILED test_models.py::test_create');
  });

  it('uses focused test-failure guidance when test failures and changed files are both present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_changed_files: ['src/foo.ts'],
        _previous_test_failure_summary: 'FAILED test_foo.py::test_bar',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Focus your retry on fixing the failing tests');
    expect(result).not.toContain('Review the files listed above');
  });

  it('renders self-review outcome when _previous_self_review_outcome is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: { issues_found: true, fixes_applied: true, categories: [] },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).toContain('found issues and applied fixes');
  });

  it('renders self-review outcome with no-issues message when fixes_applied is false', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: { issues_found: false, fixes_applied: false, categories: [] },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review ran and found no critical issues');
  });

  it('renders SR-missed categories when SR incorrectly marked them as passed', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: false,
          check_results: {
            correctness: { passed: true },
            'error-handling': { passed: true },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
    expect(result).toContain('error-handling (reviewer found 1 issue)');
    expect(result).toContain('scrutinize these categories carefully');
  });

  it('detects SR-missed categories even when category was absent from SR check_results', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: false,
          check_results: {
            correctness: { passed: true },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'test-coverage': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
    expect(result).toContain('test-coverage (reviewer found 1 issue)');
  });

  it('treats reviewer-origin failures as SR-missed even when check_results marked them failed', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'fail',
          fixes_applied: false,
          check_results: {
            correctness: { passed: false, origin: 'reviewer' },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('missed these categories');
    expect(result).toContain('correctness (reviewer found 2 issues)');
  });

  it('uses existing wording when SR correctly identified all reviewer categories', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'pass',
          fixes_applied: true,
          check_results: {
            correctness: { passed: false },
            'error-handling': { passed: false },
          },
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).toContain('found issues and applied fixes');
    expect(result).not.toContain('missed these categories');
  });

  it('uses existing wording when soft-fallback envelope (verdict=unknown, empty check_results)', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'error',
        _previous_self_review_outcome: {
          verdict: 'unknown',
          fixes_applied: false,
          check_results: {},
        },
        _first_attempt_rejection_categories: {
          correctness: 2,
          'error-handling': 1,
        },
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Self-review outcome:');
    expect(result).not.toContain('missed these categories');
  });

  it('renders previous analysis plan when _previous_analysis_plan is present', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
        _previous_analysis_plan: '## Pre-Implementation Analysis\n\nModule A depends on Module B.\nChange order: B first, then A.',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('Pre-implementation analysis from previous attempt');
    expect(result).toContain('Module A depends on Module B');
    expect(result).toContain('skip re-analysis');
  });

  it('omits analysis plan section when _previous_analysis_plan is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'test failed',
      },
    };
    const result = buildRetryContext(task);
    expect(result).not.toContain('Pre-implementation analysis from previous attempt');
  });

  it('omits retry budget when _max_attempts is absent', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 failed)');
    expect(result).not.toContain('Retry budget:');
    expect(result).not.toContain('Final attempt');
  });

  it('shows retry budget with attempts remaining when multiple retries remain', () => {
    const task = {
      context: {
        _attempt_number: 2,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #1 of 4 failed)');
    expect(result).toContain('Retry budget: attempt 2 of 4 (2 retries remaining after this one)');
    expect(result).not.toContain('Final attempt');
    expect(result).not.toContain('Be conservative');
  });

  it('warns to be conservative when exactly one retry remains after this one', () => {
    const task = {
      context: {
        _attempt_number: 3,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('attempt 3 of 4');
    expect(result).toContain('1 retry remaining after this one');
    expect(result).toContain('Be conservative');
  });

  it('flags the final attempt and urges a conservative strategy when no retries remain', () => {
    const task = {
      context: {
        _attempt_number: 4,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #3 of 4 failed)');
    expect(result).toContain('Final attempt (4 of 4)');
    expect(result).toContain('no retries remain after this one');
    expect(result).toContain('conservative strategy');
    expect(result).toContain('smallest viable change');
  });

  it('ignores inconsistent _max_attempts less than _attempt_number', () => {
    const task = {
      context: {
        _attempt_number: 5,
        _max_attempts: 4,
        _previous_failure_class: 'CODE_ERROR',
        _previous_error: 'err',
      },
    };
    const result = buildRetryContext(task);
    expect(result).toContain('## Previous Attempt (attempt #4 failed)');
    expect(result).not.toContain('Retry budget:');
    expect(result).not.toContain('Final attempt');
  });
});

// ── formatProvenanceWarnings ──────────────────────────────────────────────────
describe('formatProvenanceWarnings', () => {
  it('returns empty string when context is undefined', () => {
    expect(formatProvenanceWarnings(undefined)).toBe('');
  });

  it('returns empty string when _provenance_warnings is absent', () => {
    expect(formatProvenanceWarnings({ scope: 'medium' })).toBe('');
  });

  it('returns empty string when _provenance_warnings is an empty object', () => {
    expect(formatProvenanceWarnings({ _provenance_warnings: {} })).toBe('');
  });

  it('returns empty string when _provenance_warnings is not a dict (defensive)', () => {
    expect(formatProvenanceWarnings({ _provenance_warnings: 'oops' })).toBe('');
    expect(formatProvenanceWarnings({ _provenance_warnings: ['a'] })).toBe('');
    expect(formatProvenanceWarnings({ _provenance_warnings: null })).toBe('');
  });

  it('renders the cited-files-changed signal', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: { cited_files_changed_since_generation: true },
    });
    expect(result).toContain('## Stale-premise notice');
    expect(result).toContain('Cited files changed since the finding was generated');
    expect(result).toContain('REPRODUCE-FIRST');
  });

  it('does not render the cited-files signal when the flag is false', () => {
    expect(
      formatProvenanceWarnings({
        _provenance_warnings: { cited_files_changed_since_generation: false },
      }),
    ).toBe('');
  });

  it('renders the broken-citation count and the offending refs', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        broken_source_ref_count: 2,
        broken_refs: ['app/foo.py:42', 'app/bar.ts'],
      },
    });
    expect(result).toContain('2 broken/missing citation(s)');
    expect(result).toContain('app/foo.py:42');
    expect(result).toContain('app/bar.ts');
  });

  it('renders the broken-citation signal from refs alone when count is missing', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: { broken_refs: ['app/only.py'] },
    });
    expect(result).toContain('1 broken/missing citation(s)');
    expect(result).toContain('app/only.py');
  });

  it('renders the ungrounded-claim count and the offending refs', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        ungrounded_claim_count: 3,
        ungrounded_refs: ['claim about retries', 'claim about caching'],
      },
    });
    expect(result).toContain('3 ungrounded claim(s)');
    expect(result).toContain('claim about retries');
    expect(result).toContain('claim about caching');
  });

  it('renders all three signals combined and the no-op/pivot instruction', () => {
    const result = formatProvenanceWarnings({
      _provenance_warnings: {
        cited_files_changed_since_generation: true,
        broken_source_ref_count: 1,
        broken_refs: ['x.py'],
        ungrounded_claim_count: 1,
        ungrounded_refs: ['some claim'],
      },
    });
    expect(result).toContain('Cited files changed since the finding was generated');
    expect(result).toContain('1 broken/missing citation(s)');
    expect(result).toContain('1 ungrounded claim(s)');
    expect(result).toContain('./done --already-done');
    expect(result).toContain('do NOT fabricate');
  });

  it('does not throw on malformed nested fields and renders nothing useful', () => {
    expect(() =>
      formatProvenanceWarnings({
        _provenance_warnings: {
          broken_source_ref_count: 'two',
          broken_refs: 'notalist',
          ungrounded_refs: [123, null, 'real ref'],
        },
      }),
    ).not.toThrow();
    // ungrounded_refs has one stringifiable real ref, so the signal fires for it.
    const result = formatProvenanceWarnings({
      _provenance_warnings: { ungrounded_refs: [123, null, 'real ref'] },
    });
    expect(result).toContain('real ref');
  });
});

// ── formatInvestigateCitationProvenance ───────────────────────────────────────
describe('formatInvestigateCitationProvenance', () => {
  it('returns empty string when context is undefined', () => {
    expect(formatInvestigateCitationProvenance(undefined)).toBe('');
  });

  it('returns empty string when _provenance_warnings is absent', () => {
    expect(formatInvestigateCitationProvenance({ scope: 'medium' })).toBe('');
  });

  it('returns empty string for a non-dict _provenance_warnings (defensive)', () => {
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: 'oops' })).toBe('');
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: ['a'] })).toBe('');
    expect(formatInvestigateCitationProvenance({ _provenance_warnings: null })).toBe('');
  });

  it('returns empty string for a non-absent verdict with no stale refs', () => {
    expect(
      formatInvestigateCitationProvenance({
        _provenance_warnings: { citation_state: 'present', broken_refs: [], ungrounded_refs: [] },
      }),
    ).toBe('');
  });

  it('renders the ABSENT discount note and the stale refs', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: {
        citation_state: 'absent',
        broken_refs: ['app/gone.py'],
        ungrounded_refs: ['app/x.py:vanished'],
      },
    });
    expect(result).toContain('## Citation-provenance notice');
    expect(result).toContain('ABSENT on the current');
    expect(result).toContain('discount its actionability');
    expect(result).toContain('app/gone.py');
    expect(result).toContain('app/x.py:vanished');
    // Absent framing must not claim the defect is still actionable.
    expect(result).not.toContain('NOT presumed resolved');
  });

  it('renders the ABSENT note even when no specific stale refs are present', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: { citation_state: 'absent', broken_refs: [], ungrounded_refs: [] },
    });
    expect(result).toContain('ABSENT on the current');
    expect(result).toContain('discount its actionability');
  });

  it('renders the PARTIAL note for a grounded report with stale refs', () => {
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: {
        citation_state: 'present',
        broken_refs: ['app/moved.py'],
        ungrounded_refs: ['app/y.py:renamed'],
      },
    });
    expect(result).toContain('## Citation-provenance notice');
    expect(result).toContain('PARTIALLY grounded');
    expect(result).toContain('NOT presumed resolved');
    expect(result).toContain('remains actionable');
    expect(result).toContain('app/moved.py');
    expect(result).toContain('app/y.py:renamed');
    // Partial framing must not use the stronger absent discount.
    expect(result).not.toContain('discount its actionability');
  });

  it('does not throw on malformed nested fields', () => {
    expect(() =>
      formatInvestigateCitationProvenance({
        _provenance_warnings: {
          citation_state: 'present',
          broken_refs: 'notalist',
          ungrounded_refs: [123, null, 'real ref'],
        },
      }),
    ).not.toThrow();
    const result = formatInvestigateCitationProvenance({
      _provenance_warnings: { citation_state: 'present', ungrounded_refs: [123, null, 'real ref'] },
    });
    expect(result).toContain('real ref');
  });
});

// ── formatAcceptanceEvidence ──────────────────────────────────────────────────
describe('formatAcceptanceEvidence', () => {
  it('returns empty string when acceptance_evidence is absent or malformed', () => {
    expect(formatAcceptanceEvidence(undefined)).toBe('');
    expect(formatAcceptanceEvidence({})).toBe('');
    expect(formatAcceptanceEvidence({ acceptance_evidence: null })).toBe('');
    expect(formatAcceptanceEvidence({ acceptance_evidence: 'oops' })).toBe('');
    expect(formatAcceptanceEvidence({
      acceptance_evidence: { kind: 'named_test', test_path: 'backend/app/handler.py' },
    })).toBe('');
  });

  it('renders named_test obligations with path, test_id, and gate conditions', () => {
    const result = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'named_test',
        test_path: 'testing/test_widget.py',
        test_id: 'test_widget_handles_empty',
      },
    });

    expect(result).toContain('## Acceptance Evidence');
    expect(result).toContain('testing/test_widget.py');
    expect(result).toContain('test_widget_handles_empty');
    expect(result).toContain('real merge-test verdict');
    expect(result).toContain('production code');
    expect(result).toContain('test-only diff fails the gate');
  });

  it('renders observable_behavior and downstream_artifact obligations', () => {
    const observable = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'observable_behavior',
        description: 'the dashboard shows the live serving SHA banner',
      },
    });
    expect(observable).toContain('the dashboard shows the live serving SHA banner');
    expect(observable).toContain('real, non-empty diff');

    const artifact = formatAcceptanceEvidence({
      acceptance_evidence: {
        kind: 'downstream_artifact',
        artifact_id: 'release-deploy-artifact-registry',
      },
    });
    expect(artifact).toContain('release-deploy-artifact-registry');
    expect(artifact).toContain('real, non-empty diff');
  });
});

// ── extractModuleInterfaces ───────────────────────────────────────────────────
describe('extractModuleInterfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Re-establish defaults so overrides from prior tests don't bleed through
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('returns empty string when relatedFiles is empty', () => {
    expect(extractModuleInterfaces(null, [])).toBe('');
  });

  it('returns empty string when related files do not exist on disk', () => {
    // existsSync returns false by default
    expect(extractModuleInterfaces(null, ['backend/app/models.py'])).toBe('');
  });

  it('skips non-source files (.json, .md)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    expect(extractModuleInterfaces(null, ['package.json', 'README.md'])).toBe('');
  });

  it('skips test files (.test.ts, test_.py)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function testHelper(): void {}\n'
    );
    expect(extractModuleInterfaces(null, ['agent/src/prompt.test.ts', 'backend/testing/test_models.py'])).toBe('');
  });

  it('extracts top-level Python function signatures and class names', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n\nclass UserModel:\n    pass\n\ndef create_task(name: str, owner: str):\n    pass\n'
    );
    const result = extractModuleInterfaces(null, ['backend/app/models.py']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('backend/app/models.py');
    expect(result).toContain('def get_user(user_id: int)');
    expect(result).toContain('class UserModel');
    expect(result).toContain('def create_task(name: str, owner: str)');
  });

  it('uses __all__ as authoritative export list for Python files', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      "__all__ = ['get_user', 'create_task']\n\ndef get_user(x):\n    pass\n\ndef _private_helper(x):\n    pass\n"
    );
    const result = extractModuleInterfaces(null, ['backend/app/services.py']);
    expect(result).toContain('get_user');
    expect(result).toContain('create_task');
    // __all__ used — private helper should not appear as a standalone entry
    expect(result).not.toContain('_private_helper');
  });

  it('extracts TypeScript export declarations (function, class, interface, type, const)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(opts: Opts): string { return ""; }\n' +
      'export async function runHandler(task: Task): Promise<void> {}\n' +
      'export class TaskRunner {}\n' +
      'export interface BuildOpts { foo: string; }\n' +
      'export type TaskType = "create" | "fix";\n' +
      'export const DEFAULT_MODEL = "claude-3";\n'
    );
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('function buildPrompt');
    expect(result).toContain('function runHandler');
    expect(result).toContain('class TaskRunner');
    expect(result).toContain('interface BuildOpts');
    expect(result).toContain('type TaskType');
    expect(result).toContain('const DEFAULT_MODEL');
  });

  it('includes caller files from git grep when found', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\nagent/src/index.ts\n'
    );
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('Imported by:');
    expect(result).toContain('agent/src/handlers/standard.ts');
    expect(result).toContain('agent/src/index.ts');
  });

  it('omits Imported by section when git grep finds no callers', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    // Default mock returns Buffer.from('') — .split() throws, caught by try/catch → no callers
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).not.toContain('Imported by:');
  });

  it('handles git grep throwing gracefully (no callers shown, exports still shown)', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'export function buildPrompt(): string { return ""; }\n'
    );
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git grep failed'); });
    const result = extractModuleInterfaces(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Module Interfaces');
    expect(result).toContain('function buildPrompt');
    expect(result).not.toContain('Imported by:');
  });

  it('caps output at approximately 2KB', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    const manyExports = Array.from({ length: 80 }, (_, i) =>
      `export function veryLongFunctionName${i}(paramA: string, paramB: number): void {}`
    ).join('\n');
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(manyExports);
    const manyFiles = Array.from({ length: 15 }, (_, i) => `agent/src/module${i}.ts`);
    const result = extractModuleInterfaces(null, manyFiles);
    expect(result.length).toBeLessThanOrEqual(2000);
  });

  it('returns empty string when files have no exportable symbols', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      '// just a comment\nconst x = 1; // non-exported\n'
    );
    expect(extractModuleInterfaces(null, ['agent/src/utils.ts'])).toBe('');
  });

  it('appends Suggested Change Order when one related file imports from another', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n'
    );
    // models callers include services (in related set); services has no related callers
    vi.mocked(execFileSync)
      .mockReturnValueOnce('backend/app/services.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, ['backend/app/models.py', 'backend/app/services.py']);
    expect(result).toContain('## Suggested Change Order');
    expect(result).toContain('1. models.py');
    expect(result).toContain('2. services.py');
    expect(result).toContain('imports from models.py');
  });

  it('omits Suggested Change Order when no related files import each other', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def get_user(user_id: int):\n    pass\n'
    );
    // Both files' callers are outside the related set
    vi.mocked(execFileSync)
      .mockReturnValueOnce('other/caller.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, ['backend/app/models.py', 'backend/app/services.py']);
    expect(result).not.toContain('## Suggested Change Order');
  });

  it('appends Suggested Change Order for three-file dependency chain', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'def fn(x: int):\n    pass\n'
    );
    // models callers: [services, admin]; services callers: [admin]; admin callers: []
    vi.mocked(execFileSync)
      .mockReturnValueOnce('backend/app/services.py\nbackend/app/admin.py\n')
      .mockReturnValueOnce('backend/app/admin.py\n')
      .mockReturnValueOnce('');
    const result = extractModuleInterfaces(null, [
      'backend/app/models.py',
      'backend/app/services.py',
      'backend/app/admin.py',
    ]);
    expect(result).toContain('## Suggested Change Order');
    expect(result).toContain('1. models.py');
    expect(result).toContain('2. services.py');
    expect(result).toContain('3. admin.py');
  });
});

// ── discoverAffectedConsumers ─────────────────────────────────────────────────
describe('discoverAffectedConsumers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue('');
    vi.mocked(execFileSync).mockReturnValue(Buffer.from(''));
  });

  it('returns empty string when relatedFiles is empty', () => {
    expect(discoverAffectedConsumers(null, [])).toBe('');
  });

  it('skips test files in relatedFiles', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.test.ts', 'backend/testing/test_models.py']);
    expect(result).toBe('');
  });

  it('skips non-source files (.json, .md)', () => {
    const result = discoverAffectedConsumers(null, ['package.json', 'README.md']);
    expect(result).toBe('');
  });

  it('returns empty string when git grep finds no consumers', () => {
    // execFileSync throws (exit 1) when git grep finds no matches
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('no matches'); });
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('returns empty string when consumer files do not exist on disk', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    // existsSync returns false by default — consumer file cannot be read
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('filters out test files returned by git grep', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/prompt.test.ts\nbackend/testing/test_models.py\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });

  it('extracts named TypeScript imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { buildContextPrefix, discoverRelatedTests, extractModuleInterfaces } from \'../prompt.js\';\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('agent/src/prompt.ts');
    expect(result).toContain('agent/src/handlers/standard.ts');
    expect(result).toContain('buildContextPrefix');
    expect(result).toContain('discoverRelatedTests');
    expect(result).toContain('extractModuleInterfaces');
  });

  it('appends call-site usage counts to imported symbols', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/handlers/standard.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    // Build a file: import line at line 0, then 79 padding lines so body starts at line 80
    const importLine = 'import { foo, bar } from \'../prompt.js\';';
    const padding = Array(79).fill('').join('\n');
    const body = [
      'function example() {',
      '  foo();',
      '  foo(arg);',
      '  return foo();',
      '  const x = bar;',
      '}',
    ].join('\n');
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      `${importLine}\n${padding}\n${body}\n`
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('foo used 3x');
    expect(result).toContain('bar used 1x');
  });

  it('extracts Python imports from consumer files', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'backend/app/orchestrator.py\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'from app.services import get_task, create_task\n'
    );
    const result = discoverAffectedConsumers(null, ['backend/app/services.py']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('backend/app/services.py');
    expect(result).toContain('backend/app/orchestrator.py');
    expect(result).toContain('get_task');
    expect(result).toContain('create_task');
  });

  it('shows consumer without imports when no import line matches', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'backend/app/main.py\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    // File mentions module name but no parseable import line
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      '# uses services module indirectly\nsome_var = services_helper()\n'
    );
    const result = discoverAffectedConsumers(null, ['backend/app/services.py']);
    expect(result).toContain('backend/app/main.py');
    // No imports listed — just the consumer path
    expect(result).not.toContain('imports:');
  });

  it('includes the Affected Consumers header and description', () => {
    vi.mocked(execFileSync).mockReturnValue(
      'agent/src/index.ts\n'
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { buildFileTree } from \'./prompt.js\';\n'
    );
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toContain('## Affected Consumers');
    expect(result).toContain('Verify they remain compatible');
  });

  it('caps output at approximately 2KB', () => {
    const manyConsumers = Array.from({ length: 20 }, (_, i) =>
      `agent/src/consumers/module${i}.ts`
    ).join('\n') + '\n';
    vi.mocked(execFileSync).mockReturnValue(
      manyConsumers
    );
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync as unknown as (p: string, e: string) => string).mockReturnValue(
      'import { funcA, funcB, funcC, funcD, funcE, funcF, funcG, funcH } from \'../prompt.js\';\n'
    );
    const manyFiles = Array.from({ length: 10 }, (_, i) => `agent/src/module${i}.ts`);
    const result = discoverAffectedConsumers(null, manyFiles);
    expect(result.length).toBeLessThanOrEqual(2000);
  });

  it('handles git grep throwing gracefully and returns empty string', () => {
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git grep failed'); });
    const result = discoverAffectedConsumers(null, ['agent/src/prompt.ts']);
    expect(result).toBe('');
  });
});

// ── buildAnalysisPrompt ──────────────────────────────────────────────────────
