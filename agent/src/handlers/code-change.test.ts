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
import { writeCodeChangeHelpers } from '../prompt.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';
import * as fs from 'fs';

describe('augmentSelfReview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(git).mockReturnValue('');
  });

  it('synthesizes a stamped fallback when selfReview is null', () => {
    // augmentSelfReview never returns null — for a null/skipped self-review it
    // synthesizes an envelope and stamps prompt_version so catch-rate trends
    // can still bucket by prompt regime.
    const result = augmentSelfReview(null, 'task-123', 'base-commit');
    expect(result).not.toBeNull();
    expect(result.verdict).toBe('unknown');
    expect(result.prompt_version).toBeDefined();
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
    expectCalledWith(persistAndPostStatus,
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

describe('runCodeChangeTask promotionTarget forwarding', () => {
  const baseOpts = {
    taskId: 'task-promo-test',
    prompt: 'do the thing',
    ws: null,
    statusRef: { value: '' },
    workBranch: 'blueprint/task-promo-test',
    baseBranch: 'production',
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

  it('forwards promotionTarget to writeCodeChangeHelpers when provided', async () => {
    await runCodeChangeTask({ ...baseOpts, promotionTarget: 'production' });
    expectCalledWith(writeCodeChangeHelpers,
      expect.objectContaining({ promotionTarget: 'production' }),
    );
  });

  it('forwards undefined promotionTarget when not provided', async () => {
    await runCodeChangeTask({ ...baseOpts });
    expectCalledWith(writeCodeChangeHelpers,
      expect.objectContaining({ promotionTarget: undefined }),
    );
  });
});

// ── runCodeChangeTask — no-op / already-done completion safeguard ─────────────
// The ONLY clean path to a no-PR COMPLETED is the affirmative
// `./done --already-done` verb (which writes /tmp/done_<id>). A bare no-diff
// clean exit with NO marker must fail as `incomplete`, never silently complete.
describe('runCodeChangeTask redundant-no-op completion safeguard', () => {
  const baseOpts = {
    taskId: 'noop-handler-task',
    prompt: 'add Alembic migration for PoolVm registry',
    ws: null,
    statusRef: { value: '' },
    workBranch: 'blueprint/noop-handler-task',
    baseBranch: 'plan/3a47191b',
    owner: 'owner',
    repo: 'repo',
    testsOnMainBranch: true,
    baseCommit: 'abc123',
    noChangesMessage: 'No migration changes generated.',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.writeFileSync).mockImplementation(() => {});
    vi.mocked(fs.unlinkSync).mockImplementation(() => {});
    // No diff: status clean AND HEAD unchanged from baseCommit.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'abc123';
      return '';
    });
  });

  it('completes cleanly (no FAILED) when ./done --already-done wrote the done marker', async () => {
    // Claude affirmatively signalled via ./done --already-done → /tmp/done_<id>.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) =>
      String(p) === '/tmp/done_noop-handler-task',
    );
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    // The wrapper must NOT post any terminal status itself — ./done already did.
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalled();
  });

  it('FAILS as incomplete on a bare no-diff clean exit with NO affirmative ./done --already-done', async () => {
    // No markers at all: agent produced no diff but never affirmatively signalled.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'thought about it, did nothing', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    // It must FAIL (incomplete) — NOT silently COMPLETE despite noChangesMessage being set.
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('incomplete');
    // And it must NOT have posted a COMPLETED with the no-changes message.
    const completedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'COMPLETED',
    );
    expect(completedCalls).toHaveLength(0);
    // The failure reason must point the agent at the affirmative verb.
    expect(String(payload.logs)).toContain('./done --already-done');
  });

  it('FAILS as claude_code_failed when the subprocess exited non-zero with no diff', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 1, allOutput: 'crashed', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('claude_code_failed');
  });
});
