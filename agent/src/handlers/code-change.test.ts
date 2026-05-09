import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    git: vi.fn(),
    sendLog: vi.fn(),
    persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '' }),
    rotateOAuthCredentialsIfChanged: vi.fn().mockResolvedValue(undefined),
  };
});

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return { ...actual, writeCodeChangeHelpers: vi.fn() };
});

vi.mock('fs', () => {
  const stub = {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  };
  return { ...stub, default: stub };
});

// ── Imports under test ────────────────────────────────────────────────────────
import { augmentSelfReview, runCodeChangeTask } from './code-change.js';
import { git, persistAndPostStatus, runClaudeCode } from '../index.js';
import * as fs from 'fs';

describe('augmentSelfReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(git).mockReturnValue('');
  });

  it('returns null when selfReview is null', () => {
    const result = augmentSelfReview(null, 'task-123', 'base-commit');
    expect(result).toBeNull();
  });

  it('returns selfReview unchanged when test baseline exists', () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    const selfReview = { verdict: 'pass', check_results: {} };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');
    expect(result).toEqual(selfReview);
    expect(vi.mocked(git)).not.toHaveBeenCalled();
  });

  it('returns selfReview unchanged when regression_risk already exists', () => {
    const selfReview = {
      verdict: 'pass',
      check_results: { regression_risk: { passed: true, issue: 'existing' } },
    };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');
    expect(result).toEqual(selfReview);
    expect(vi.mocked(git)).not.toHaveBeenCalled();
  });

  it('injects regression_risk when missing and heuristic signals pass', () => {
    const smallDiff = 'diff --git a/src/file.ts b/src/file.ts\n+line1\n+line2';
    vi.mocked(git).mockReturnValue(smallDiff);
    const selfReview = { verdict: 'pass', check_results: {} };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');

    expect(result).not.toBeNull();
    const regression = (result.check_results as Record<string, unknown>).regression_risk as Record<string, unknown>;
    expect(regression.passed).toBe(true);
    expect(regression.issue as string).toContain('[heuristic-derived]');
  });

  it('injects regression_risk with passed=false when high-risk signals detected', () => {
    // Large diff with no tests: 80+ source lines, 0 test lines
    const largeDiff = Array.from({ length: 85 }, (_, i) =>
      i < 85 ? `+src_line_${i}` : `+test_line_${i}`
    ).join('\n');
    vi.mocked(git).mockReturnValue(largeDiff);
    const selfReview = { verdict: 'pass', check_results: {} };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');

    expect(result).not.toBeNull();
    const regression = (result.check_results as Record<string, unknown>).regression_risk as Record<string, unknown>;
    expect(regression.passed).toBe(false);
    expect(regression.issue as string).toContain('source lines / 0 test lines');
  });

  it('returns selfReview unchanged on git error', () => {
    vi.mocked(git).mockImplementation(() => {
      throw new Error('git failed');
    });
    const selfReview = { verdict: 'pass', check_results: {} };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');
    expect(result).toEqual(selfReview);
  });

  it('preserves existing check_results when adding regression_risk', () => {
    const smallDiff = 'diff --git a/src/file.ts b/src/file.ts\n+line1';
    vi.mocked(git).mockReturnValue(smallDiff);
    const selfReview = {
      verdict: 'pass',
      check_results: {
        correctness: { passed: true, issue: 'looks good' },
        'type-safety': { passed: false, issue: 'missing null check' },
      },
    };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');

    expect(result.check_results).toHaveProperty('correctness');
    expect(result.check_results).toHaveProperty('type-safety');
    expect(result.check_results).toHaveProperty('regression_risk');
  });

  it('handles selfReview with missing check_results property', () => {
    const smallDiff = 'diff --git a/src/file.ts b/src/file.ts\n+line1';
    vi.mocked(git).mockReturnValue(smallDiff);
    const selfReview = { verdict: 'pass' };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');

    expect(result.check_results).toBeDefined();
    expect(result.check_results).toHaveProperty('regression_risk');
  });

  it('detects test files correctly and counts them separately', () => {
    // Diff with both test and src changes; should not flag as risky
    const mixedDiff = `
diff --git a/src/main.ts b/src/main.ts
${Array.from({ length: 90 }, (_, i) => `+line${i}`).join('\n')}
diff --git a/src/main.test.ts b/src/main.test.ts
+test line
+test line
+test line
`.trim();
    vi.mocked(git).mockReturnValue(mixedDiff);
    const selfReview = { verdict: 'pass', check_results: {} };
    const result = augmentSelfReview(selfReview, 'task-123', 'base-commit');

    expect(result).not.toBeNull();
    const regression = (result.check_results as Record<string, unknown>).regression_risk as Record<string, unknown>;
    // Should pass because there ARE test changes
    expect(regression.passed).toBe(true);
  });
});

describe('runCodeChangeTask SCOPE_EMPTY guard', () => {
  const baseOpts = {
    taskId: 'task-scope-guard',
    prompt: 'do the thing',
    ws: null,
    statusRef: { value: '' },
    workBranch: 'blueprint/task-scope-guard',
    baseBranch: 'main',
    owner: 'owner',
    repo: 'repo',
    testsOnMainBranch: true,
    baseCommit: 'abc123',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.writeFileSync).mockImplementation(() => {});
    vi.mocked(fs.unlinkSync).mockImplementation(() => {});
    vi.mocked(git).mockReturnValue('abc123');
  });

  it('exits with SCOPE_EMPTY when all related_files are covered by completed siblings', async () => {
    await runCodeChangeTask({
      ...baseOpts,
      autoDecomposed: true,
      relatedFiles: ['agent/src/handlers/code-change.ts'],
      siblingTasks: [{ status: 'COMPLETED', files: ['agent/src/handlers/code-change.ts'], title: 'cluster 1/5' }],
    });
    expect(vi.mocked(runClaudeCode)).not.toHaveBeenCalled();
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledWith(
      'task-scope-guard',
      expect.objectContaining({ failure_category: 'SCOPE_EMPTY', status: 'BLOCKED_PRECONDITION' }),
    );
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(String(payload.logs)).toMatch(/SCOPE_EMPTY:/);
    expect(String(payload.logs)).toMatch(/cluster 1\/5/);
  });

  it('skips guard when autoDecomposed is false', async () => {
    await runCodeChangeTask({
      ...baseOpts,
      autoDecomposed: false,
      relatedFiles: ['agent/src/handlers/code-change.ts'],
      siblingTasks: [{ status: 'COMPLETED', files: ['agent/src/handlers/code-change.ts'] }],
    });
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
    const scopeEmptyCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].failure_category === 'SCOPE_EMPTY',
    );
    expect(scopeEmptyCalls).toHaveLength(0);
  });

  it('skips guard when only some related_files are covered by siblings', async () => {
    await runCodeChangeTask({
      ...baseOpts,
      autoDecomposed: true,
      relatedFiles: ['agent/src/handlers/code-change.ts', 'agent/src/handlers/standard.ts'],
      siblingTasks: [{ status: 'COMPLETED', files: ['agent/src/handlers/code-change.ts'] }],
    });
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
  });

  it('skips guard when no sibling task is COMPLETED', async () => {
    await runCodeChangeTask({
      ...baseOpts,
      autoDecomposed: true,
      relatedFiles: ['agent/src/handlers/code-change.ts'],
      siblingTasks: [{ status: 'IN_PROGRESS', files: ['agent/src/handlers/code-change.ts'] }],
    });
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
  });

  it('skips guard when siblingTasks is not provided', async () => {
    await runCodeChangeTask({
      ...baseOpts,
      autoDecomposed: true,
      relatedFiles: ['agent/src/handlers/code-change.ts'],
    });
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalled();
  });
});
