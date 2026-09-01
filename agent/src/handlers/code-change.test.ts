import { describe, it, expect, vi, beforeEach } from 'vitest';
import { expectCalledWith } from '../utils/typed-mock-call.js';

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
  taskOwnedOpenPullRequest,
} from '../task-checkpoint.js';
import { writePrHelper } from '../prompts/pr-helper.js';
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
    const continuationPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(continuationPrompt).toContain(baseOpts.prompt);
    expect(continuationPrompt).toContain(`task_id=${taskId}`);
    expect(continuationPrompt).toContain(`repo=${baseOpts.owner}/${baseOpts.repo}`);
    expect(continuationPrompt).toContain(`work_branch=${baseOpts.workBranch}`);
    expect(continuationPrompt).toContain('changed_files=agent/src/handlers/code-change.ts');
    expect(continuationPrompt).toContain('finish every required validation step');
    expect(continuationPrompt).toContain('Do not publish before validating');
    expect(continuationPrompt).not.toContain('RIGHT NOW, run ./pr');
    // The done marker wins → the bash ./done already posted COMPLETED, so the
    // wrapper must NOT post any terminal status itself, and definitely not FAILED.
    const failedCalls = vi.mocked(persistAndPostStatus).mock.calls.filter(
      c => c[1].status === 'FAILED',
    );
    expect(failedCalls).toHaveLength(0);
  });

  it('preserves dedicated task text surrounded by large production prompt sections', async () => {
    const taskRequest = 'TASK-SPECIFIC: preserve the acceptance criteria';
    const longPrompt = `${'generic stable guidance '.repeat(500)}\n${taskRequest}\n${'repo map and artifact context '.repeat(500)}`;
    let continued = false;
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'waiting', lastResultText: '' })
      .mockImplementationOnce(() => {
        continued = true;
        return Promise.resolve({ exitCode: 0, allOutput: '', lastResultText: '' });
      });
    vi.mocked(fs.existsSync).mockImplementation((path: fs.PathLike) =>
      continued && (String(path) === prUrlMarker || String(path) === doneMarker),
    );
    vi.mocked(fs.readFileSync).mockImplementation((path: fs.PathOrFileDescriptor) =>
      String(path) === prUrlMarker ? validPrUrl : '',
    );

    await runCodeChangeTask({ ...baseOpts, prompt: longPrompt, taskPrompt: taskRequest });

    const continuationPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(continuationPrompt).toContain(taskRequest);
    expect(continuationPrompt).not.toContain('generic stable guidance');
    expect(continuationPrompt).not.toContain('repo map and artifact context');
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

describe('task-owned open PR linkage', () => {
  it('accepts only a complete canonical PR number and branch pair', () => {
    expect(taskOwnedOpenPullRequest({
      pr_number: 42,
      branch: 'blueprint/task-1234abcd',
    })).toEqual({ number: 42, branch: 'blueprint/task-1234abcd' });
    expect(taskOwnedOpenPullRequest({
      pr_number: '42',
      branch: 'blueprint/task-1234abcd',
    })).toEqual({ number: 42, branch: 'blueprint/task-1234abcd' });
  });

  it.each([
    {},
    { pr_number: 0, branch: 'blueprint/task-1234abcd' },
    { pr_number: 'not-a-number', branch: 'blueprint/task-1234abcd' },
    { pr_number: 42 },
    { pr_number: 42, branch: '' },
  ])('rejects incomplete or malformed linkage %#', (payload) => {
    expect(taskOwnedOpenPullRequest(payload)).toBeUndefined();
  });
});

// ── durable unshipped-work checkpoint / retry transition ─────────────────────
describe('durable unshipped-work checkpoint retry transition', () => {
  const taskId = 'checkpoint-retry-task';
  const workBranch = 'blueprint/task-checkpoint';
  const baseCommit = 'abc1234567890';
  const localCheckpointSha = '1111111111111111111111111111111111111111';
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
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return '';
      if (args[0] === 'ls-remote') return '';
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
    expectCalledWith(gitAsync, [
      'push', `--force-with-lease=refs/heads/${workBranch}:`, 'origin',
      `HEAD:refs/heads/${workBranch}`,
    ]);
    expectCalledWith(apiGet,
      `/api/agent/github/token?task_id=${taskId}&force_refresh=true`,
    );
    expectCalledWith(writeGitTokenFile, 'fresh-checkpoint-token');

    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      ([, payload]) => payload.status === 'FAILED',
    );
    expect(failedCall?.[1].failure_category).toBe('incomplete');
    expect(vi.mocked(gitAsync).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(persistAndPostStatus).mock.invocationCallOrder[0],
    );
  });

  it.each(['blocked', 'fail'] as const)(
    'does not redundantly checkpoint after the generated ./%s helper wrote its marker',
    async (terminalVerb) => {
      let markerVisible = false;
      vi.mocked(fs.existsSync).mockImplementation(path =>
        markerVisible && String(path) === `/tmp/${terminalVerb}_${taskId}`,
      );
      vi.mocked(runClaudeCode).mockImplementationOnce(() => {
        markerVisible = true;
        return Promise.resolve({
          exitCode: 0,
          allOutput: `./${terminalVerb} posted after the publication gate failed`,
          lastResultText: '',
        });
      });

      await runCodeChangeTask({ ...baseOpts });

      expect(vi.mocked(runClaudeCode)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(gitAsync)).not.toHaveBeenCalled();
      expect(vi.mocked(git).mock.calls.some(([args]) => args[0] === 'commit')).toBe(false);
      // The terminal helper already posted status. The wrapper's responsibility
      // here is only to accept the marker, not checkpoint or transition again.
      expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalled();
    },
  );

  it('reconciles a lost checkpoint-push response when remote already equals local', async () => {
    let remoteRead = 0;
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M agent/src/handlers/code-change.ts';
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return '';
      if (args[0] === 'ls-remote') {
        remoteRead += 1;
        return remoteRead === 1 ? '' : `${localCheckpointSha}\trefs/heads/${workBranch}`;
      }
      return '';
    });
    vi.mocked(gitAsync).mockRejectedValueOnce(new Error('push response lost'));

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(gitAsync)).toHaveBeenCalledTimes(1);
    const failedCall = vi.mocked(persistAndPostStatus).mock.calls.find(
      ([, payload]) => payload.status === 'FAILED',
    );
    expect(failedCall?.[1].failure_category).toBe('incomplete');
    expect(failedCall?.[1].commit_hash).toBe(localCheckpointSha);
    expect(failedCall?.[1].storage_uri).toBe(`git-ref:refs/heads/${workBranch}`);
  });

  it('fails closed when a competing remote SHA appears and never overwrites it', async () => {
    const competingSha = '2222222222222222222222222222222222222222';
    let remoteRead = 0;
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M agent/src/handlers/code-change.ts';
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return '';
      if (args[0] === 'ls-remote') {
        remoteRead += 1;
        const sha = remoteRead === 1 ? null : competingSha;
        return sha ? `${sha}\trefs/heads/${workBranch}` : '';
      }
      return '';
    });
    vi.mocked(gitAsync).mockRejectedValueOnce(new Error('lease rejected'));

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(gitAsync)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalled();
  });

  it('does not post retryable terminal status when the initial remote lease is unavailable', async () => {
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M agent/src/handlers/code-change.ts';
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return '';
      if (args[0] === 'ls-remote') throw new Error('remote unavailable');
      return '';
    });

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(gitAsync)).not.toHaveBeenCalled();
    expect(vi.mocked(persistAndPostStatus)).not.toHaveBeenCalled();
  });

  it('reuses an already-durable helper checkpoint without another commit or push', async () => {
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return '';
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return buildTaskCheckpointCommitMessage(taskId, baseCommit);
      if (args[0] === 'ls-remote') {
        return `${localCheckpointSha}\trefs/heads/${workBranch}`;
      }
      return '';
    });

    await runCodeChangeTask({ ...baseOpts });

    // Clean checkpoint HEAD still differs from baseCommit, so it is unshipped
    // work; idempotent durability recognition must avoid stacking a commit.
    expect(vi.mocked(git).mock.calls.some(([args]) => args[0] === 'commit')).toBe(false);
    expect(vi.mocked(gitAsync)).not.toHaveBeenCalled();
    expectCalledWith(persistAndPostStatus,
      taskId,
      expect.objectContaining({ status: 'FAILED', failure_category: 'incomplete' }),
    );
  });

  it('advances an existing checkpoint when the retry added a new dirty edit', async () => {
    const priorCheckpointSha = '3333333333333333333333333333333333333333';
    vi.mocked(git).mockImplementation((args: string[]) => {
      if (args[0] === 'status') return ' M work.txt';
      if (args[0] === 'rev-parse') return localCheckpointSha;
      if (args[0] === 'show') return buildTaskCheckpointCommitMessage(taskId, baseCommit);
      if (args[0] === 'ls-remote') {
        return `${priorCheckpointSha}\trefs/heads/${workBranch}`;
      }
      return '';
    });

    await runCodeChangeTask({ ...baseOpts });

    expect(vi.mocked(git).mock.calls.some(([args]) => args[0] === 'commit')).toBe(true);
    expectCalledWith(gitAsync, [
      'push',
      `--force-with-lease=refs/heads/${workBranch}:${priorCheckpointSha}`,
      'origin',
      `HEAD:refs/heads/${workBranch}`,
    ]);
    expectCalledWith(persistAndPostStatus,
      taskId,
      expect.objectContaining({ status: 'FAILED', failure_category: 'incomplete' }),
    );
  });

  it('fails closed when the remote branch probe fails for transport or auth reasons', async () => {
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') throw new Error('authentication failed');
      return '';
    });
    const listOpen = vi.fn().mockResolvedValue([]);

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      { git: gitOps, listOpenPullRequestsForBranch: listOpen, sendLog: vi.fn() },
    )).rejects.toThrow(/refusing retry without an authoritative remote read/);

    expect(listOpen).not.toHaveBeenCalled();
    expect(gitOps.mock.calls.some(([args]) => args[0] === 'checkout')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('preserves an unowned non-checkpoint branch instead of deleting it', async () => {
    const remoteHead = '4'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${remoteHead}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return remoteHead;
      if (args[0] === 'show') return 'ordinary commit without task checkpoint trailers';
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/refusing automatic deletion/);

    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args[0] === 'checkout')).toBe(false);
  });

  it('refuses checkpoint inspection when the branch advances after the PR read', async () => {
    const observedHead = '4'.repeat(40);
    const fetchedHead = '5'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${observedHead}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return fetchedHead;
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/advanced while checking for retry ownership/);

    expect(gitOps.mock.calls.some(([args]) => args[0] === 'show')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('refuses a matching branch PR without canonical full-task linkage', async () => {
    const openHead = '4'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${openHead}\trefs/heads/${workBranch}`;
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([{
          number: 42,
          html_url: 'https://github.com/owner/repo/pull/42',
          head: { ref: workBranch, sha: openHead, repo: { full_name: 'owner/repo' } },
          base: { ref: 'main' },
        }]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/cannot be proven to match the exact task branch/);

    expect(gitOps.mock.calls.some(([args]) => args[0] === 'fetch')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('resumes the exact single open PR owned by the retrying task', async () => {
    const openHead = '4'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${openHead}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return openHead;
      return '';
    });
    const listOpen = vi.fn().mockResolvedValue([{
      number: 42,
      html_url: 'https://github.com/owner/repo/pull/42',
      head: { ref: workBranch, sha: openHead, repo: { full_name: 'owner/repo' } },
      base: { ref: 'main' },
    }]);
    const log = vi.fn();

    const result = await prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
        ownedOpenPullRequest: { number: 42, branch: workBranch },
      },
      { git: gitOps, listOpenPullRequestsForBranch: listOpen, sendLog: log },
    );

    expect(result).toEqual({
      restoredCheckpoint: false,
      resumedOpenPullRequest: {
        number: 42,
        htmlUrl: 'https://github.com/owner/repo/pull/42',
      },
    });
    expect(gitOps).toHaveBeenCalledWith([
      'fetch', '--no-tags', '--depth=100', 'origin',
      `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
    ]);
    expect(gitOps).toHaveBeenCalledWith([
      'checkout', '-b', workBranch, '--track', `origin/${workBranch}`,
    ]);
    expect(gitOps).toHaveBeenCalledWith([
      'config', 'blueprint.taskRemoteLeaseSha', openHead,
    ]);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
    expect(log).toHaveBeenCalledWith(
      null,
      expect.stringContaining(`Resuming open PR #42 for task ${taskId} at exact head ${openHead}`),
    );
  });

  it.each([
    ['head branch', { head: { ref: 'foreign-branch' } }],
    ['head repository', { head: { repo: { full_name: 'other/repo' } } }],
    ['base branch', { base: { ref: 'release' } }],
    ['observed head', { head: { sha: '5'.repeat(40) } }],
  ])('refuses an open PR whose %s does not match the exact retry identity', async (_label, override) => {
    const openHead = '4'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${openHead}\trefs/heads/${workBranch}`;
      return '';
    });
    const canonical = {
      number: 42,
      html_url: 'https://github.com/owner/repo/pull/42',
      head: { ref: workBranch, sha: openHead, repo: { full_name: 'owner/repo' } },
      base: { ref: 'main' },
    };
    const candidate = {
      ...canonical,
      ...override,
      head: { ...canonical.head, ...('head' in override ? override.head : {}) },
      base: { ...canonical.base, ...('base' in override ? override.base : {}) },
    };

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
        ownedOpenPullRequest: { number: 42, branch: workBranch },
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([candidate]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/cannot be proven to match the exact task branch/);

    expect(gitOps.mock.calls.some(([args]) => args[0] === 'fetch')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args[0] === 'checkout')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('refuses when the task branch advances between PR read and fetch', async () => {
    const openHead = '4'.repeat(40);
    const advancedHead = '5'.repeat(40);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${openHead}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return advancedHead;
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
        ownedOpenPullRequest: { number: 42, branch: workBranch },
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([{
          number: 42,
          html_url: 'https://github.com/owner/repo/pull/42',
          head: { ref: workBranch, sha: openHead, repo: { full_name: 'owner/repo' } },
          base: { ref: 'main' },
        }]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/head changed while preparing retry/);

    expect(gitOps.mock.calls.some(([args]) => args[0] === 'checkout')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('restores the checkpoint as retry work and keeps publication force-with-lease capable', async () => {
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${'d'.repeat(40)}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return 'd'.repeat(40);
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
    expect(gitOps).toHaveBeenCalledWith([
      'config', 'blueprint.taskRemoteLeaseSha', 'd'.repeat(40),
    ]);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);

    // The durable remote checkpoint intentionally remains as the lease target.
    // Publication must bind its lease to the exact observed remote SHA rather
    // than using Git's ambient tracking state or a plain non-fast-forward push.
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
      `_PR_REMOTE_REF="refs/heads/${workBranch}"`,
    );
    expect(prScript as string).toContain(
      '_PR_LEASE="--force-with-lease=$_PR_REMOTE_REF:$_PR_EXPECTED_REMOTE_SHA"',
    );
    expect(prScript as string).toContain(
      'git push "$_PR_LEASE" origin "HEAD:$_PR_REMOTE_REF"',
    );
    expect(prScript as string).not.toContain(
      `git push --force-with-lease origin "${workBranch}"`,
    );
  });

  // A task retried long after its checkpoint was written can have its merge base
  // outside BOTH the shallow clone (DEFAULT_CLONE_DEPTH) and the --depth=100
  // checkpoint fetch. Observed in production on NeurIPS task 0396aaee: the merge
  // base sat 213 commits behind main, `git merge --squash` died with
  // "refusing to merge unrelated histories", and because git refuses before
  // touching the index the worktree stayed clean — so the conflict-preserving
  // branch rethrew and all three retries reproduced it byte for byte.
  it('deepens a shallow clone when the checkpoint merge base is out of reach', async () => {
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    let deepened = false;
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${'d'.repeat(40)}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return 'd'.repeat(40);
      if (args[0] === 'show') return checkpointMessage;
      if (args[0] === 'rev-parse' && args[1] === '--is-shallow-repository') {
        return deepened ? 'false' : 'true';
      }
      if (args[0] === 'merge-base') {
        if (!deepened) throw new Error('fatal: no merge base found');
        return 'a'.repeat(40);
      }
      if (args[0] === 'fetch' && args.includes('--unshallow')) {
        deepened = true;
        return '';
      }
      if (args[0] === 'merge' && args[1] === '--squash') {
        // Real git's behaviour: it refuses outright while the base is unreachable.
        if (!deepened) throw new Error('fatal: refusing to merge unrelated histories');
        return '';
      }
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
    expect(gitOps).toHaveBeenCalledWith(['fetch', '--no-tags', '--unshallow', 'origin']);
    expect(gitOps).toHaveBeenCalledWith([
      'fetch', '--no-tags', 'origin',
      `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
    ]);
    expect(gitOps).toHaveBeenCalledWith([
      'merge', '--squash', '--no-commit', `origin/${workBranch}`,
    ]);
    // The remote checkpoint must survive: it is the lease target for ./pr.
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });

  it('refuses a checkpoint branch that advances during the deepening refetch', async () => {
    const checkpointHead = 'd'.repeat(40);
    const advancedHead = 'e'.repeat(40);
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    let deepened = false;
    let branchRefetchCount = 0;
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${checkpointHead}\trefs/heads/${workBranch}`;
      if (args[0] === 'show') return checkpointMessage;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) {
        return branchRefetchCount >= 2 ? advancedHead : checkpointHead;
      }
      if (args[0] === 'rev-parse' && args[1] === '--is-shallow-repository') return 'true';
      if (args[0] === 'merge-base') {
        if (!deepened) throw new Error('fatal: no merge base found');
        return 'a'.repeat(40);
      }
      if (args[0] === 'fetch' && args.includes('--unshallow')) {
        deepened = true;
        return '';
      }
      if (args[0] === 'fetch' && args.includes(`+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`)) {
        // The first fetch materializes A; the deepening helper's second fetch
        // observes competing B.
        branchRefetchCount += 1;
        return '';
      }
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/advanced while deepening its history/);

    expect(gitOps.mock.calls.some(([args]) => args[0] === 'merge')).toBe(false);
    expect(gitOps).not.toHaveBeenCalledWith([
      'config', 'blueprint.taskRemoteLeaseSha', expect.any(String),
    ]);
  });

  it('does not deepen when the checkpoint merge base is already reachable', async () => {
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${'d'.repeat(40)}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return 'd'.repeat(40);
      if (args[0] === 'show') return checkpointMessage;
      if (args[0] === 'rev-parse' && args[1] === '--is-shallow-repository') return 'true';
      if (args[0] === 'merge-base') return 'b'.repeat(40);
      return '';
    });

    const result = await prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
        sendLog: vi.fn(),
      },
    );

    expect(result.restoredCheckpoint).toBe(true);
    // The common retry keeps paying only the cheap --depth=100 fetch.
    expect(gitOps.mock.calls.some(([args]) => args.includes('--unshallow'))).toBe(false);
  });

  it('refuses the squash restore when histories stay unrelated after deepening', async () => {
    const checkpointMessage = buildTaskCheckpointCommitMessage(taskId, baseCommit);
    const gitOps = vi.fn((args: string[]): string => {
      if (args[0] === 'ls-remote') return `${'d'.repeat(40)}\trefs/heads/${workBranch}`;
      if (args[0] === 'rev-parse' && args[1] === `origin/${workBranch}`) return 'd'.repeat(40);
      if (args[0] === 'show') return checkpointMessage;
      if (args[0] === 'rev-parse' && args[1] === '--is-shallow-repository') return 'true';
      if (args[0] === 'merge-base') throw new Error('fatal: no merge base found');
      return '';
    });

    await expect(prepareTaskWorkBranch(
      {
        owner: 'owner', repo: 'repo', ghToken: 'token', baseBranch: 'main',
        workBranch, taskId, ws: null,
      },
      {
        git: gitOps,
        listOpenPullRequestsForBranch: vi.fn().mockResolvedValue([]),
        sendLog: vi.fn(),
      },
    )).rejects.toThrow(/shares no history with main/);

    // Squashing unrelated trees would fabricate a diff — it must never be attempted.
    expect(gitOps.mock.calls.some(([args]) => args[0] === 'merge')).toBe(false);
    expect(gitOps.mock.calls.some(([args]) => args.includes('--delete'))).toBe(false);
  });
});
