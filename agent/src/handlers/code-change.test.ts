import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    apiGet: vi.fn().mockResolvedValue({ data: { token: 'fresh-checkpoint-token' } }),
    git: vi.fn(),
    gitAsync: vi.fn().mockResolvedValue(''),
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

vi.mock('../git-credential.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../git-credential.js')>();
  return { ...actual, writeGitTokenFile: vi.fn() };
});

// ── Imports under test ────────────────────────────────────────────────────────
import { augmentSelfReview, runCodeChangeTask } from './code-change.js';
import { apiGet, git, gitAsync, persistAndPostStatus, runClaudeCode } from '../index.js';
import { writeGitTokenFile } from '../git-credential.js';
import { writeCodeChangeHelpers } from '../prompt.js';
import {
  buildTaskCheckpointCommitMessage,
  prepareTaskWorkBranch,
} from '../task-checkpoint.js';
import { writePrHelper } from '../prompts/pr-helper.js';
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

// ── runCodeChangeTask — forced terminal-verb continuation backstop ────────────
// A passive end_turn (Claude exits 0 with an uncommitted diff but NO terminal
// marker) must trigger exactly ONE forced continuation that demands a terminal
// verb, then re-inspect the markers. Only if the continuation STILL produces no
// terminal verb / PR does the task fail as `incomplete`.
describe('runCodeChangeTask forced terminal-verb continuation', () => {
  const taskId = 'passive-endturn-task';
  const baseOpts = {
    taskId,
    prompt: 'add a deterministic backstop',
    ws: null,
    statusRef: { value: '' },
    workBranch: `blueprint/${taskId}`,
    baseBranch: 'main',
    owner: 'owner',
    repo: 'repo',
    testsOnMainBranch: true,
    baseCommit: 'abc123',
  };
  const prUrlMarker = `/tmp/pr_url_${taskId}`;
  const doneMarker = `/tmp/done_${taskId}`;
  const validPrUrl = 'https://github.com/owner/repo/pull/123';

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.writeFileSync).mockImplementation(() => {});
    vi.mocked(fs.unlinkSync).mockImplementation(() => {});
    // Uncommitted diff present: git status returns a porcelain line → hasChanges.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M agent/src/handlers/code-change.ts';
      if (args[0] === 'rev-parse') return 'abc123';
      if (args[0] === 'diff') return 'agent/src/handlers/code-change.ts';
      return '';
    });
  });

  it('completes when the forced continuation ships a PR after a passive end_turn', async () => {
    // First run: passive end_turn — exit 0, a diff, NO markers. The forced
    // continuation then opens a PR (writes /tmp/pr_url_<id>) and calls ./done
    // (writes /tmp/done_<id>); the wrapper must end COMPLETED, not FAILED.
    let continued = false;
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: "I've scheduled a check. Waiting for the gate.", lastResultText: '' })
      .mockImplementationOnce(() => { continued = true; return Promise.resolve({ exitCode: 0, allOutput: '', lastResultText: '' }); });
    // Markers only appear AFTER the continuation runs.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) =>
      continued && (String(p) === prUrlMarker || String(p) === doneMarker),
    );
    vi.mocked(fs.readFileSync).mockImplementation((p: fs.PathOrFileDescriptor) =>
      String(p) === prUrlMarker ? validPrUrl : '',
    );

    await runCodeChangeTask({ ...baseOpts });

    // The continuation must have fired exactly once (two total runClaudeCode calls).
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    // The done marker wins → the bash ./done already posted COMPLETED, so the
    // wrapper must NOT post any terminal status itself, and definitely not FAILED.
    const failedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'FAILED',
    );
    expect(failedCalls).toHaveLength(0);
  });

  it('does NOT finalize COMPLETED when a PR-URL marker is present but base..HEAD nets to zero effective diff', async () => {
    // Defense-in-depth: a rebased/superseded branch can carry a valid PR-URL
    // marker yet contribute NO effective diff vs base. The wrapper must NOT post
    // COMPLETED on Claude's behalf — it falls through to the fail-closed path so
    // the no-op is recorded as an explicit incomplete disposition, not success.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) =>
      String(p) === prUrlMarker,
    );
    vi.mocked(fs.readFileSync).mockImplementation((p: fs.PathOrFileDescriptor) =>
      String(p) === prUrlMarker ? validPrUrl : '',
    );
    // Empty base..HEAD diff (successful git call → confirmed empty), clean tree,
    // HEAD unchanged from baseCommit — so there is genuinely nothing shipped.
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'abc123';
      if (args[0] === 'diff') return '';
      return '';
    });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'rebased away to nothing', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    // No COMPLETED — and specifically no COMPLETED carrying the PR URL.
    const completedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'COMPLETED',
    );
    expect(completedCalls).toHaveLength(0);
    // The task fails closed as incomplete.
    const failedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'FAILED',
    );
    expect(failedCalls).toHaveLength(1);
    expect(failedCalls[0][1].failure_category).toBe('incomplete');
  });

  it('completes via the PR-URL finalizer when the continuation opens a PR but skips ./done', async () => {
    // Continuation opens a PR (valid /tmp/pr_url_<id>) but ends without ./done.
    // The wrapper must finalise COMPLETED on Claude's behalf.
    let continued = false;
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'waiting for the monitor', lastResultText: '' })
      .mockImplementationOnce(() => { continued = true; return Promise.resolve({ exitCode: 0, allOutput: '', lastResultText: '' }); });
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) =>
      continued && String(p) === prUrlMarker,
    );
    vi.mocked(fs.readFileSync).mockImplementation((p: fs.PathOrFileDescriptor) =>
      String(p) === prUrlMarker ? validPrUrl : '',
    );

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const completedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'COMPLETED',
    );
    expect(completedCalls).toHaveLength(1);
    expect(completedCalls[0][1].pr_url).toBe(validPrUrl);
  });

  it('FAILS as incomplete when the forced continuation still produces no terminal marker', async () => {
    // Both the original run AND the forced continuation end with a diff but no
    // marker. After the single capped continuation, the task must fail closed.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'still just prose, no verb', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    // Exactly one forced continuation — capped, no loop.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(persistAndPostStatus)).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('incomplete');
  });

  it('does NOT issue a continuation when there is no diff (nothing to rescue)', async () => {
    // No diff: git status clean AND HEAD unchanged. The passive-rescue path must
    // not fire — a no-diff exit has no work to ship, so it fails as incomplete
    // via the existing safeguard without a second runClaudeCode call.
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return 'abc123';
      return '';
    });
    vi.mocked(runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: 'did nothing', lastResultText: '' });

    await runCodeChangeTask({ ...baseOpts });

    // Only the original run — no forced continuation when there's no diff.
    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(payload.status).toBe('FAILED');
    expect(payload.failure_category).toBe('incomplete');
  });
});

// ── durable unshipped-work checkpoint / retry transition ─────────────────────
describe('durable unshipped-work checkpoint retry transition', () => {
  const taskId = 'checkpoint-retry-task';
  const workBranch = 'blueprint/task-checkpoint';
  const baseCommit = 'abc1234567890';
  const baseOpts = {
    taskId,
    prompt: 'make the requested code change',
    ws: null,
    statusRef: { value: '' },
    workBranch,
    baseBranch: 'main',
    owner: 'owner',
    repo: 'repo',
    testsOnMainBranch: true,
    baseCommit,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(fs.writeFileSync).mockImplementation(() => {});
    vi.mocked(fs.unlinkSync).mockImplementation(() => {});
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M agent/src/handlers/code-change.ts';
      if (args[0] === 'rev-parse') return baseCommit;
      return '';
    });
    vi.mocked(gitAsync).mockResolvedValue('');
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'ended without a terminal verb',
      lastResultText: '',
    });
  });

  it('durably pushes a provenance checkpoint before posting incomplete', async () => {
    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(2);
    const commitCall = vi.mocked(git).mock.calls.find(([args]) => args[0] === 'commit');
    expect(commitCall).toBeDefined();
    expect(commitCall?.[0]).toContain('--allow-empty');
    expect(commitCall?.[0].join('\n')).toContain(`blueprint-checkpoint: preserve unshipped task ${taskId}`);
    expect(commitCall?.[0].join('\n')).toContain(`Blueprint-Base-Commit: ${baseCommit}`);
    expect(vi.mocked(gitAsync)).toHaveBeenCalledWith([
      'push', '--force-with-lease', 'origin',
      `HEAD:refs/heads/${workBranch}`,
    ]);
    expect(vi.mocked(apiGet)).toHaveBeenCalledWith(
      `/api/agent/github/token?task_id=${taskId}&force_refresh=true`,
    );
    expect(vi.mocked(writeGitTokenFile)).toHaveBeenCalledWith('fresh-checkpoint-token');

    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      ([, payload]) => payload.status === 'FAILED',
    );
    expect(failedCall?.[1].failure_category).toBe('incomplete');
    expect(vi.mocked(gitAsync).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(persistAndPostStatus).mock.invocationCallOrder[0],
    );
  });

  it('restores the checkpoint as retry work and keeps publication force-with-lease capable', async () => {
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `deadbeef\trefs/heads/${workBranch}`;
      if (args[0] === 'show') return checkpointMessage;
      return '';
    });
    const listOpen = vi.fn().mockResolvedValue([]);
    const log = vi.fn();

    const result = await prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      { git: gitOps, listOpenPullRequestsForBranch: listOpen, sendLog: log },
    );

    expect(result.restoredCheckpoint).toBe(true);
    expect(gitOps).toHaveBeenCalledWith([
      'fetch', '--no-tags', '--depth=100', 'origin',
      `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
    ]);
    expect(gitOps).toHaveBeenCalledWith(['checkout', '-b', workBranch]);
    expect(gitOps).toHaveBeenCalledWith([
      'merge', '--squash', '--no-commit', `origin/${workBranch}`,
    ]);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);

    // The durable remote checkpoint intentionally remains as the lease target.
    // The generated ./pr publication path must replace it with force-with-lease,
    // not a plain non-fast-forward push that would wedge the recovered retry.
    vi.mocked(fs.writeFileSync).mockClear();
    writePrHelper({
      taskId,
      workBranch,
      baseBranch: 'main',
      owner: 'owner',
      repo: 'repo',
      testsOnMainBranch: true,
    });
    const prWrite = vi.mocked(fs.writeFileSync).mock.calls.find(
      ([path]) => String(path).endsWith('/pr'),
    );
    expect(prWrite).toBeDefined();
    const prScript = prWrite?.[1];
    expect(typeof prScript).toBe('string');
    expect(prScript as string).toContain(
      `git push --force-with-lease origin "${workBranch}"`,
    );
  });
});
