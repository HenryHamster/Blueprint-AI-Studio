import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
// 2026-05-04: handleFix no longer drives lint/test/push/PR via wrapper helpers.
// It builds an enrichedPrompt (iteration header, scope convergence, CI failure
// context, dialog-system prompt) and delegates everything post-prompt to
// runCodeChangeTask. Tests:
//   • Mock runCodeChangeTask (default no-op) and inspect its `prompt` opt to
//     verify the various sections the handler is supposed to inject.
//   • Keep the rebase / conflict-marker / stale-premise pre-Claude logic
//     covered — those remain active production code that runs before
//     runCodeChangeTask.
//   • Delete the wrapper-driven tests: pushAndCreatePR, lintFixLoop,
//     post-rebase lint re-validation, conflict-marker retry failure, PR body
//     annotation after push (patchPrBodyMeta is gone).
//
// handleReview is unchanged by the rewrite — it still calls runClaudeCode
// directly with the review prompt — so the bulk of the existing handleReview
// tests pass through unchanged.
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sanitize: (s: string) => s,
  sendLog: vi.fn(),
  log: vi.fn(),
  getAgentToken: vi.fn().mockReturnValue(''),
  apiPost: vi.fn(),
  apiGet: vi.fn().mockResolvedValue({ data: { token: 'fresh' } }),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  readHandlerOutput: vi.fn(),
  validateBranchName: vi.fn((b: string) => b),
  git: vi.fn().mockReturnValue(''),
  gitAsync: vi.fn().mockResolvedValue(''),
  gitCommit: vi.fn().mockReturnValue(true),
  setupWorkspace: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  captureTestBaseline: vi.fn().mockResolvedValue(null),
  testFixLoop: vi.fn().mockResolvedValue({ passed: true, testFailuresDetected: false, failureSummary: '' }),
  extractPytestSelectorsFromOutput: vi.fn((output: string) => {
    const match = output.match(/FAILED\s+(backend\/testing\/\S+\.py::\S+)/);
    return match ? [match[1].replace(/\s+-.*$/, '')] : [];
  }),
  snapshotToolTrace: vi.fn().mockReturnValue([]),
  extractInvestigatedFiles: vi.fn().mockReturnValue([]),
  cloneWithRetry: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../workspacePrep.js', () => ({
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    getTaskTypeHints: vi.fn().mockReturnValue({}),
    readClaudeMd: vi.fn().mockReturnValue(''),
    formatProjectContext: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
    writePlaybookHelper: vi.fn(),
    writeMessagesHelper: vi.fn(),
    writeCodeChangeHelpers: vi.fn(),
    formatPlaybook: vi.fn().mockReturnValue(''),
    buildRetryContext: vi.fn().mockReturnValue(''),
    discoverRelatedTests: vi.fn().mockReturnValue(''),
    formatSelfReviewBlindSpots: vi.fn().mockReturnValue(''),
  };
});

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue({}),
}));

// Mock the code-change module so handleFix tests can assert on what was
// passed to runCodeChangeTask without exercising marker-detection branches.
vi.mock('./code-change.js', () => ({
  runCodeChangeTask: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('fs', () => {
  const stub = {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
  };
  return { ...stub, default: stub };
});

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
  execFile: vi.fn(),
  execSync: vi.fn().mockReturnValue(''),
  spawn: vi.fn().mockReturnValue({
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
}));

import fs from 'fs';
import { execFileSync } from 'child_process';
import * as indexMod from '../index.js';
import {
  computeMaxFixIterations, handleReview, handleFix, REVIEW_FEEDBACK_HISTORY_CHARS,
  annotateResolvedIssues, detectMediumScopeSubsystemHint, pushForceWithLeaseRetrying,
  detectMigrationConflict, buildMigrationConflictGuidance, buildBlueprintPrFooter,
  formatTrajectorySection, formatDriftSection, verifyStepsOk,
} from './review-fix.js';
import { runCodeChangeTask } from './code-change.js';

const statusRef = () => ({ value: '' });

/** Asserts a value is defined and returns it (narrowed). Replaces unsafe `x!` in tests. */
function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}

/** Returns the prompt arg passed to the latest runCodeChangeTask invocation
 *  (handleFix delegates to it). */
function capturedFixPrompt(): string {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0].prompt;
}

/** Returns the full opts object passed to the latest runCodeChangeTask invocation. */
function capturedFixOpts(): Parameters<typeof runCodeChangeTask>[0] {
  const calls = vi.mocked(runCodeChangeTask).mock.calls;
  if (calls.length === 0) throw new Error('runCodeChangeTask was not invoked');
  return calls[calls.length - 1][0];
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fs.existsSync).mockReturnValue(false);
  vi.mocked(fs.readFileSync).mockReturnValue('');
  vi.mocked(execFileSync).mockReturnValue('');
  vi.mocked(indexMod.git).mockReturnValue('');
  vi.mocked(indexMod.gitAsync).mockResolvedValue('');
  vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  vi.mocked(indexMod.persistAndPostStatus).mockResolvedValue(undefined);
  vi.mocked(indexMod.apiGet).mockResolvedValue({ data: { token: 'fresh' } });
  vi.mocked(indexMod.testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false, failureSummary: '' });
  vi.mocked(indexMod.captureTestBaseline).mockResolvedValue(null);
  // fetch is used by handleReview for PR metadata
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ title: 'PR title', body: 'PR body' }),
  }) as unknown as typeof fetch;
});

describe('computeMaxFixIterations — mirrors backend _max_review_iterations', () => {
  it('returns 8 for ci_failure fix_reason regardless of scope', () => {
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'ci_failure' } },
    })).toBe(8);
    expect(computeMaxFixIterations({
      context: { scope: 'large', review_loop: { fix_reason: 'ci_failure' } },
    })).toBe(8);
  });

  it('returns 10 for small scope / non-ci_failure', () => {
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'code_review' } },
    })).toBe(10);
    expect(computeMaxFixIterations({ context: { scope: 'small' } })).toBe(10);
  });

  it('returns 12 for medium scope', () => {
    expect(computeMaxFixIterations({ context: { scope: 'medium' } })).toBe(12);
  });

  it('returns 16 for large or unknown scope', () => {
    expect(computeMaxFixIterations({ context: { scope: 'large' } })).toBe(16);
    expect(computeMaxFixIterations({ context: { scope: 'xlarge' } })).toBe(16);
  });

  it('defaults missing scope to medium (matches backend ctx.get("scope", "medium"))', () => {
    expect(computeMaxFixIterations({ context: {} })).toBe(12);
    expect(computeMaxFixIterations({})).toBe(12);
  });

  it('handles task.context present but review_loop missing', () => {
    // Falls through to scope-based branch; no throw.
    expect(computeMaxFixIterations({ context: { scope: 'small' } })).toBe(10);
    expect(computeMaxFixIterations({ context: { scope: 'large' } })).toBe(16);
  });

  it('handles review_loop present but fix_reason missing', () => {
    // Optional-chain ensures fix_reason is undefined, not throwing.
    expect(computeMaxFixIterations({
      context: { scope: 'medium', review_loop: { iteration: 3 } },
    })).toBe(12);
  });

  it('prefers backend-provided max_iterations over scope-based default', () => {
    // Backend may reduce cap (~30%) for low-quality directions; agent must use it
    // so "iteration N of M" and FINAL-attempt warning match what backend enforces.
    expect(computeMaxFixIterations({
      context: { scope: 'medium', review_loop: { max_iterations: 8 } },
    })).toBe(8);
    // Backend value also wins over ci_failure default.
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { fix_reason: 'ci_failure', max_iterations: 6 } },
    })).toBe(6);
    // Invalid backend values fall through to scope-based default.
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { max_iterations: 0 } },
    })).toBe(10);
    expect(computeMaxFixIterations({
      context: { scope: 'small', review_loop: { max_iterations: 'bad' } },
    })).toBe(10);
  });

  it('does not throw on unusual (but typed) context shapes', () => {
    // Empty object is the realistic edge; the guard coerces to ctx={}.
    expect(() => computeMaxFixIterations({ context: {} })).not.toThrow();
    expect(() => computeMaxFixIterations({})).not.toThrow();
  });
});

describe('annotateResolvedIssues — marks issues not re-flagged by next iteration', () => {
  it('annotates an issue with resolvedInIteration when it does not reappear next', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check', file: 'a.ts' },
        { severity: 'minor', description: 'unused import' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'missing null check', file: 'a.ts' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    const issues1 = defined(it1.issues);
    const nullCheck = defined(issues1.find(i => i.description === 'missing null check'));
    const unused = defined(issues1.find(i => i.description === 'unused import'));
    expect(nullCheck.resolvedInIteration).toBeUndefined();
    expect(unused.resolvedInIteration).toBe(2);
  });

  it('never annotates the most recent iteration', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [{ severity: 'major', description: 'x' }] },
      { iteration: 2, feedback: '', issues: [{ severity: 'major', description: 'y' }] },
    ]);
    const it2 = defined(result.find(e => e.iteration === 2));
    expect(defined(it2.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('normalizes whitespace/case when matching descriptions', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [{ severity: 'major', description: 'Missing  NULL check' }] },
      { iteration: 2, feedback: '', issues: [{ severity: 'major', description: 'missing null check' }] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('leaves feedback-only (no issues) entries untouched', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: 'plain text' },
      { iteration: 2, feedback: 'also plain' },
    ]);
    expect(result[0].issues).toBeUndefined();
  });

  it('treats rephrased issues with high token overlap as still-present (not resolved)', () => {
    // The example from the task description: same conceptual issue, reworded.
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'null check not present' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('treats reordered/rephrased multi-word issues as still-present', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'Missing null check in handler' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'Handler lacks null check' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('still marks unrelated issues as resolved when next iteration has different problems', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'minor', description: 'add unit tests for parser' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBe(2);
  });
});

// ── Review history text truncation: reviewer and fixer must align ───────────
describe('review history feedback truncation — reviewer sees at least as much as fixer', () => {
  const longFeedback = 'x'.repeat(3000);
  const histWithStructured = [{
    iteration: 1,
    feedback: longFeedback,
    issues: [{ severity: 'major', description: 'real problem', file: 'a.ts', line: 10 }],
  }];
  const histNoStructured = [{ iteration: 1, feedback: longFeedback }];

  it('reviewer truncates fallback feedback at the shared constant (no structured issues)', async () => {
    await handleReview(
      'task-trunc-review',
      { pr_number: 5, branch: 'feat/y', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'medium', review_loop: { iteration: 2, review_history: histNoStructured } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    const iterMatch = prompt.match(/- Iteration 1: (x+)/);
    expect(iterMatch?.[1]?.length).toBe(REVIEW_FEEDBACK_HISTORY_CHARS);
  });

  it('fixer truncates fallback feedback at the same constant (no structured issues)', async () => {
    await handleFix(
      'task-trunc-fix',
      { branch: 'feat/y', base_branch: 'main', pr_number: 5, prompt: 'address review' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: histNoStructured } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    const iterMatch = prompt.match(/- Iteration 1: (x+)/);
    expect(iterMatch?.[1]?.length).toBe(REVIEW_FEEDBACK_HISTORY_CHARS);
  });

  it('reviewer prefers structured issues over raw feedback (no truncation applied)', async () => {
    await handleReview(
      'task-structured-review',
      { pr_number: 6, branch: 'feat/z', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'medium', review_loop: { iteration: 2, review_history: histWithStructured } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('[MAJOR] [a.ts:10] real problem');
    // fallback text must NOT appear when structured issues exist
    expect(prompt).not.toMatch(/- Iteration 1: x+/);
  });

  it('fixer prefers structured issues over raw feedback (no truncation applied)', async () => {
    await handleFix(
      'task-structured-fix',
      { branch: 'feat/z', base_branch: 'main', pr_number: 6, prompt: 'address review' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithStructured } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('[MAJOR] [a.ts:10] real problem');
    expect(prompt).not.toMatch(/- Iteration 1: x+/);
  });
});

// ── handleReview: scope-overflow replaces (not augments) scope preamble ─────
describe('handleReview — scope overflow replaces scope-narrowed preamble', () => {
  const basePayload = {
    pr_number: 50,
    branch: 'feature/big',
    base_branch: 'main',
    original_prompt: '',
  };

  // Build a `git diff --name-status` style listing with N files.
  const fakeDiffNameStatus = (n: number) =>
    Array.from({ length: n }, (_, i) => `M\tsrc/file${i}.ts`).join('\n');

  it('small-scope PR exceeding threshold gets overflow preamble WITHOUT "lower priority" text', async () => {
    // 8 files > small threshold of 6.
    vi.mocked(indexMod.git).mockImplementation(((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(8);
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-overflow-small', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    // Overflow note present
    expect(prompt).toContain('exceeds the typical range for a small-scope change');
    expect(prompt).toContain('review ALL checklist sections thoroughly');
    // Contradictory small-scope deprioritization MUST be gone
    expect(prompt).not.toContain('Security and performance sections are lower priority');
    expect(prompt).not.toContain('This is a small-scope change. Focus your review');
  });

  it('small-scope PR within threshold keeps the small-scope preamble (no overflow note)', async () => {
    // 3 files within small threshold of 6.
    vi.mocked(indexMod.git).mockImplementation(((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(3);
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-no-overflow', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('Security and performance sections are lower priority');
    expect(prompt).not.toContain('exceeds the typical range');
  });

  it('medium-scope PR exceeding threshold replaces empty preamble with overflow note', async () => {
    // 12 files > medium threshold of 10.
    vi.mocked(indexMod.git).mockImplementation(((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(12);
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-overflow-medium', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('exceeds the typical range for a medium-scope change');
  });
});

// ── detectMediumScopeSubsystemHint: subsystem-aware priority guidance ───────
describe('detectMediumScopeSubsystemHint — medium-scope subsystem hint', () => {
  it('returns agent-focused hint when all changed files are under agent/', () => {
    const hint = detectMediumScopeSubsystemHint('M\tagent/src/handlers/standard.ts\nM\tagent/src/prompt.ts');
    expect(hint).toContain('agent/');
    expect(hint).toContain('deprioritize SQL injection');
  });

  it('returns admin-ui-focused hint when all changed files are under admin-ui/', () => {
    const hint = detectMediumScopeSubsystemHint('M\tadmin-ui/src/App.tsx\nA\tadmin-ui/src/components/Foo.tsx');
    expect(hint).toContain('admin-ui/');
    expect(hint).toContain('Deprioritize backend-specific checks');
  });

  it('keeps full security scrutiny for backend-only changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/app/orchestrator.py\nM\tbackend/app/main.py');
    expect(hint).toContain('backend/');
    expect(hint).toContain('full security scrutiny');
  });

  it('deprioritizes security for backend/testing-only changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/testing/test_foo.py\nA\tbackend/testing/test_bar.py');
    expect(hint).toContain('test');
    expect(hint).toContain('Deprioritize');
    expect(hint).not.toContain('full security scrutiny');
  });

  it('keeps full security scrutiny when backend changes include a non-test file', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/testing/test_foo.py\nM\tbackend/app/main.py');
    expect(hint).toContain('full security scrutiny');
  });

  it('returns cross-system guidance when changes span 2+ subsystems', () => {
    const hint = detectMediumScopeSubsystemHint('M\tagent/src/index.ts\nM\tbackend/app/main.py');
    expect(hint).toContain('cross-system');
    expect(hint).toContain('agent');
    expect(hint).toContain('backend');
    expect(hint).toContain('interface consistency');
  });

  it('returns cross-system guidance for backend + admin-ui changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/app/main.py\nM\tadmin-ui/src/App.tsx');
    expect(hint).toContain('cross-system');
    expect(hint).toContain('admin-ui');
    expect(hint).toContain('backend');
  });

  it('returns empty string for changed files placeholder / no files', () => {
    expect(detectMediumScopeSubsystemHint('')).toBe('');
    expect(detectMediumScopeSubsystemHint('(could not generate file list)')).toBe('');
  });
});

describe('handleReview — medium-scope subsystem hint integration', () => {
  it('injects agent-focused hint into the review prompt for medium-scope agent-only PRs', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/handlers/review-fix.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-medium-agent',
      { pr_number: 10, branch: 'feat/agent-thing', base_branch: 'main', original_prompt: 'do agent thing' },
      { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('confined to agent/');
    expect(prompt).toContain('deprioritize SQL injection');
  });

  it('injects cross-system hint for multi-subsystem medium-scope PRs', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/index.ts\nM\tbackend/app/main.py';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-medium-mixed',
      { pr_number: 11, branch: 'feat/mixed', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('confined to agent/');
    expect(prompt).not.toContain('confined to admin-ui/');
    expect(prompt).not.toContain('confined to backend/');
    expect(prompt).toContain('cross-system');
    expect(prompt).toContain('interface consistency');
  });

  it('does not emit medium-scope hint for small-scope PRs', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/handlers/review-fix.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-small-agent',
      { pr_number: 12, branch: 'feat/small', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('confined to agent/');
    // small-scope still gets its own hint
    expect(prompt).toContain('small-scope change');
  });
});

// ── handleReview: self-review blind-spots surfacing ─────────────────────────
describe('handleReview — self-review blind-spots surfacing', () => {
  it('passes task.context._self_review_outcome and the changed-file path list to formatSelfReviewBlindSpots, and injects the returned section into the prompt', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('SELF_REVIEW_BLIND_SPOTS_MARKER\n\n');
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/prompt.ts\nA\tagent/src/handlers/review-fix.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    const outcome = {
      verdict: 'PASS',
      categories: ['incomplete_review'],
      truncated_files_read: ['agent/src/prompt.ts'],
    };
    await handleReview(
      'task-blind-spots',
      { pr_number: 77, branch: 'feat/blind', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small', _self_review_outcome: outcome } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(promptMod.formatSelfReviewBlindSpots).toHaveBeenCalledWith(
      outcome,
      ['agent/src/prompt.ts', 'agent/src/handlers/review-fix.ts'],
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('SELF_REVIEW_BLIND_SPOTS_MARKER');
  });

  it('passes undefined self-review outcome when task.context is absent', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('');
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\ta.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-no-context',
      { pr_number: 78, branch: 'feat/nctx', base_branch: 'main', original_prompt: '' },
      {},
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(promptMod.formatSelfReviewBlindSpots).toHaveBeenCalledWith(undefined, ['a.ts']);
  });
});

// ── handleReview: PR changed_files emission ─────────────────────────────────
describe('handleReview — emits changed_files for downstream drift detection', () => {
  it('includes changed_files in persistAndPostStatus so the next fix-spawn drift detector has fresh PR state', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tbackend/app/main.py\nA\tbackend/app/new.py';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-changed-files',
      { pr_number: 600, branch: 'feat/cf', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const calls = vi.mocked(indexMod.persistAndPostStatus).mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    const lastCall = calls[calls.length - 1];
    const payload = lastCall[1] as { changed_files?: string[] };
    // Without my audit fix, this would be undefined and review_history entries
    // and drift_signal current_files would all fall back to stale orig data.
    expect(payload.changed_files).toEqual(['backend/app/main.py', 'backend/app/new.py']);
  });

  it('omits changed_files when the diff command yielded nothing parseable', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        // The handler defaults to "(could not generate file list)" on failure;
        // simulate that path by returning the same sentinel.
        return '(could not generate file list)';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-no-diff',
      { pr_number: 601, branch: 'feat/no-diff', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const calls = vi.mocked(indexMod.persistAndPostStatus).mock.calls;
    const lastCall = calls[calls.length - 1];
    const payload = lastCall[1] as { changed_files?: string[] };
    // When the diff list is unusable, omit the field rather than ship a
    // sentinel-string that downstream consumers would treat as a real path.
    expect(payload.changed_files).toBeUndefined();
  });
});

// ── handleReview: planned-files classifier (four-category parity) ───────────
describe('handleReview — unplanned-file four-category classifier', () => {
  it('replaces the old "flag as MAJOR unless directly necessary" line with the four-category classifier when planned files are configured and the diff contains an unplanned non-test file', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        // planned: src/auth.ts; unplanned non-test: src/main.ts
        return 'M\tagent/src/auth.ts\nM\tagent/src/main.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-classifier',
      { pr_number: 401, branch: 'feat/classifier', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small', _planned_files: ['agent/src/auth.ts'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    // Old harsh phrasing must be gone.
    expect(prompt).not.toContain('flag them as MAJOR unless they are directly necessary to fix a listed review issue');
    // All four categories must appear so the reviewer can classify.
    expect(prompt).toContain('scope-creep');
    expect(prompt).toContain('caller-impact');
    expect(prompt).toContain('discovered-dependency');
    expect(prompt).toContain('cluster-boundary-correction');
    // Skepticism guard must be present so reviewer doesn't take the agent's stated reason at face value.
    expect(prompt).toContain('Apply skepticism');
    // The unplanned file must still be listed so the classifier has something to act on.
    expect(prompt).toContain('agent/src/main.ts');
  });

  it('shows the "all in plan" message instead of the classifier when no unplanned non-test files are present', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/auth.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-allplanned',
      { pr_number: 402, branch: 'feat/allplanned', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small', _planned_files: ['agent/src/auth.ts'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('All non-test changed files are in the original plan');
    expect(prompt).not.toContain('Classify each unplanned non-test file');
  });

  it('does not emit the planned-files section when the task has no _planned_files context', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/main.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-noplan',
      { pr_number: 403, branch: 'feat/noplan', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('Planned files from the original analysis plan');
    expect(prompt).not.toContain('Classify each unplanned non-test file');
  });

  it('classifier omits unplanned TEST-path files (test paths are not in scope-creep scope)', async () => {
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/auth.ts\nA\tagent/src/auth.test.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await handleReview(
      'task-test-unplanned',
      { pr_number: 404, branch: 'feat/test-unplanned', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small', _planned_files: ['agent/src/auth.ts'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    // Test files are explicitly excluded from the unplanned-non-test list, so the
    // classifier section should not fire on this PR.
    expect(prompt).toContain('All non-test changed files are in the original plan');
    expect(prompt).not.toContain('Classify each unplanned non-test file');
  });
});

// ── handleReview: verdict-file parsing ───────────────────────────────────────
describe('handleReview — verdict file parsing', () => {
  const basePayload = {
    pr_number: 123,
    branch: 'feature/x',
    base_branch: 'main',
    original_prompt: 'do the thing',
  };

  it('reads verdict JSON from /tmp file and forwards it as verdict_json', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      '{"verdict":"APPROVE","feedback":"looks good"}',
    );
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: 'review output', lastResultText: 'review text',
    });

    await handleReview(
      'task-1', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const call = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0];
    expect(call[0]).toBe('task-1');
    const status = call[1];
    expect(status.status).toBe('COMPLETED');
    expect(status.verdict_json).toBe('{"verdict":"APPROVE","feedback":"looks good"}');
    expect(String(status.logs)).toContain('"verdict":"APPROVE"');
  });

  it("augments verdict_json with investigated_files derived from the review's tool trace", async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      '{"verdict":"REQUEST_CHANGES","feedback":"x","issues":[]}',
    );
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });
    vi.mocked(indexMod.extractInvestigatedFiles).mockReturnValueOnce([
      'admin-ui/src/components/DirectionCard.tsx',
      'admin-ui/src/components/DirectionCard.test.tsx',
    ]);

    await handleReview(
      'task-investigated', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as { verdict: string; investigated_files: string[] };
    expect(verdictJson.verdict).toBe('REQUEST_CHANGES');
    expect(verdictJson.investigated_files).toEqual([
      'admin-ui/src/components/DirectionCard.tsx',
      'admin-ui/src/components/DirectionCard.test.tsx',
    ]);
  });

  it('falls back to output parsing when verdict file contains malformed JSON', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('not json at all');
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: 'some claude output', lastResultText: 'last text',
    });

    await handleReview(
      'task-2', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.status).toBe('COMPLETED');
    expect(status.verdict_json).toBeUndefined();
    expect(String(status.logs)).toBe('last text');
  });

  it('falls back to output parsing when verdict file contains JSON without verdict field', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"feedback":"orphan"}');
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: 'out', lastResultText: 'last',
    });

    await handleReview(
      'task-3', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.verdict_json).toBeUndefined();
    expect(String(status.logs)).toBe('last');
  });

  it('passes review taskType, scope, and direction_id to formatProjectContext', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatProjectContext).mockClear();
    await handleReview(
      'task-pc',
      basePayload,
      { context: { scope: 'small', direction_id: 'd-abc123' }, project_context: { foo: 'bar' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(promptMod.formatProjectContext).toHaveBeenCalledWith(
      { foo: 'bar' }, 'review', 'small', 'd-abc123',
    );
  });

  it('surfaces FAILED status with failure_category when Claude exits non-zero', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: 'boom', lastResultText: '',
    });

    await handleReview(
      'task-4', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.status).toBe('FAILED');
    expect(status.failure_category).toBe('claude_code_failed');
    expect(status.artifact_type).toBe('ERROR_LOG');
  });
});

// ── handleFix: prompt enrichment + runCodeChangeTask delegation ─────────────
//
// 2026-05-04: handleFix's wrapper-driven post-Claude pipeline (push, retry-
// with-lease, lint re-validation, conflict-marker retry-then-fail, PR body
// annotation) was replaced with runCodeChangeTask. The handler now: clones,
// rebases, optionally enriches the prompt for conflicts/stale-premise/CI
// failures/iteration headers/scope-pressure, then delegates to
// runCodeChangeTask. Tests inspect the prompt arg and the rest of the opts.
describe('handleFix — runCodeChangeTask delegation', () => {
  const basePayload = {
    branch: 'feature/x',
    base_branch: 'main',
    pr_number: 42,
    prompt: 'address review feedback',
  };

  it('forwards the right opts (existingPrNumber wired so ./pr open updates the existing PR)', async () => {
    await handleFix(
      'task-fix-opts',
      basePayload,
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
    const opts = capturedFixOpts();
    expect(opts.taskId).toBe('task-fix-opts');
    expect(opts.workBranch).toBe('feature/x');
    expect(opts.baseBranch).toBe('main');
    expect(opts.owner).toBe('owner');
    expect(opts.repo).toBe('repo');
    // The defining wiring: existingPrNumber tells ./pr open to push to the
    // existing PR's branch instead of creating a new one.
    expect(opts.existingPrNumber).toBe(42);
    expect(opts.testsOnMainBranch).toBe(true);
    expect(opts.noChangesMessage).toBe('No file changes after review-fix iteration.');
  });

  it('sets testsOnMainBranch=false when base_branch is not main', async () => {
    await handleFix(
      'task-fix-feature-base',
      { ...basePayload, base_branch: 'feature/parent' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('feature/parent');
    expect(opts.testsOnMainBranch).toBe(false);
  });

  it('throws when prompt is missing — never calls runCodeChangeTask', async () => {
    await expect(
      handleFix(
        'task-fix-noprompt',
        { branch: 'feature/x', base_branch: 'main', pr_number: 42 },
        { context: { scope: 'small' } },
        'gh-token', 'owner', 'repo', null, statusRef(),
      ),
    ).rejects.toThrow(/Fix task missing prompt/);
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();
  });

  it('runs fresh-reviewer pass when project_metadata.is_self_repo is true and enable_fresh_reviewer is unset', async () => {
    vi.mocked(indexMod.git).mockReturnValue(Array.from({ length: 25 }, (_, i) => `+line ${i}`).join('\n'));
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });

    await handleFix(
      'task-fix-self-repo',
      basePayload,
      {
        context: { scope: 'small' },
        project_metadata: { is_self_repo: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const reviewerCall = vi.mocked(indexMod.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('You are reviewing a PR'),
    );
    expect(reviewerCall).toBeDefined();
  });

  it('forwards project_context into the fresh-reviewer calibration block when enabled', async () => {
    vi.mocked(indexMod.git).mockReturnValue(Array.from({ length: 25 }, (_, i) => `+line ${i}`).join('\n'));
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '{"reviewer_findings":[]}' });

    await handleFix(
      'task-fix-reviewer-calibration',
      basePayload,
      {
        context: { scope: 'small' },
        project_context: {
          self_review_catch_rate: {
            categories: {
              security: {
                miss_rate: 0.5,
                weighted_miss_rate: 0.8,
                examples: [{ severity: 'HIGH', desc: 'missing auth guard' }],
              },
            },
          },
        },
        project_metadata: { enable_fresh_reviewer: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const reviewerPrompt = vi.mocked(indexMod.runClaudeCode).mock.calls.find(
      call => typeof call[0] === 'string' && call[0].includes('Calibration from prior self-review misses:'),
    )?.[0];
    if (typeof reviewerPrompt !== 'string') throw new Error('expected reviewer prompt');
    expect(reviewerPrompt).toContain('- security: 50% missed (80% severity-weighted)');
    expect(reviewerPrompt).toContain('missing auth guard');
  });

  it('rebases onto base branch before delegating to runCodeChangeTask', async () => {
    const seenGitArgs: string[][] = [];
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args) seenGitArgs.push(args);
      return '';
    }) as unknown as typeof indexMod.git);

    await handleFix(
      'task-fix-rebase',
      basePayload,
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // The handler fetches the base branch and rebases onto it as part of
    // pre-Claude conflict resolution. Both must run.
    const fetchCall = seenGitArgs.find(a => a[0] === 'fetch' && a.some(x => x.includes('refs/heads/main')));
    const rebaseCall = seenGitArgs.find(a => a[0] === 'rebase' && a[1] === 'origin/main');
    expect(fetchCall).toBeDefined();
    expect(rebaseCall).toBeDefined();
    // Implementation must follow.
    expect(vi.mocked(runCodeChangeTask)).toHaveBeenCalledTimes(1);
  });
});

// ── handleFix: self-review blind-spots surfacing ────────────────────────────
describe('handleFix — self-review blind-spots surfacing', () => {
  it('passes task.context._self_review_outcome and the changed-file path list to formatSelfReviewBlindSpots, and prepends the returned section to the prompt', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('FIX_BLIND_SPOTS_MARKER\n\n');
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-only')) {
        return 'agent/src/prompts/self-review.ts\nagent/src/handlers/review-fix.ts';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    const outcome = {
      verdict: 'PASS',
      blind_spots: ['unsure about the retry path'],
    };
    await handleFix(
      'task-fix-blind',
      { branch: 'feature/x', base_branch: 'main', pr_number: 99, prompt: 'address review feedback' },
      { context: { scope: 'small', _self_review_outcome: outcome } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(promptMod.formatSelfReviewBlindSpots).toHaveBeenCalledWith(
      outcome,
      ['agent/src/prompts/self-review.ts', 'agent/src/handlers/review-fix.ts'],
    );
    expect(capturedFixPrompt()).toContain('FIX_BLIND_SPOTS_MARKER');
  });

  it('falls back to _planned_files + related_files when the diff is empty (no materialized PR diff)', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('');
    // git diff --name-only returns '' (default mock) — fallback path triggers.

    await handleFix(
      'task-fix-fallback',
      { branch: 'feature/x', base_branch: 'main', pr_number: 100, prompt: 'address review feedback', related_files: ['payload-related.ts'] },
      { context: { scope: 'small', _planned_files: ['planned-a.ts'], related_files: ['ctx-related.ts'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(promptMod.formatSelfReviewBlindSpots).toHaveBeenCalledWith(
      undefined,
      ['planned-a.ts', 'ctx-related.ts'],
    );
  });

  it('does not prepend anything when formatSelfReviewBlindSpots returns ""', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('');

    await handleFix(
      'task-fix-empty',
      { branch: 'feature/x', base_branch: 'main', pr_number: 101, prompt: 'address review feedback' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(capturedFixPrompt()).not.toContain('Author-flagged uncertainty');
    expect(capturedFixPrompt()).not.toContain('Self-review coverage');
  });
});

describe('handleFix — code-change helper guidance is in prompt', () => {
  // The prompt now MUST tell Claude about ./check + ./pr + ./done + ./fail. If
  // CODE_CHANGE_HELPER_GUIDANCE drops out of handleFix, Claude has no way to
  // ship its work — silent regression that would only show up in production.
  // Lock it in.
  it('includes ./check, ./pr, ./done, ./fail guidance in the enriched prompt', async () => {
    await handleFix(
      'task-fix-helpers',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('./check');
    expect(prompt).toContain('./pr');
    expect(prompt).toContain('./done');
    expect(prompt).toContain('./fail');
  });
});

// ── handleFix: conflict marker + stale-premise detection ────────────────────
describe('handleFix — conflict marker detection', () => {
  it('enriches the prompt with a CONFLICT MARKERS FOUND header when git grep finds markers', async () => {
    // No unmerged paths (git mock returns ''), but grep finds marker-bearing files.
    vi.mocked(execFileSync).mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) {
        return 'backend/app/foo.py\nagent/src/bar.ts\n';
      }
      return '';
    }) as unknown as typeof execFileSync);

    await handleFix(
      'task-fix-1',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'please fix the regression' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // runCodeChangeTask was invoked with the marker-enriched prompt.
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('CONFLICT MARKERS FOUND');
    expect(prompt).toContain('backend/app/foo.py');
    expect(prompt).toContain('agent/src/bar.ts');
  });
});

describe('handleFix — baseline capture', () => {
  // captureTestBaseline still runs PRE-Claude in the medium/large scope code-
  // review path. It's a side-effect (warms the test runner, surfaces pre-
  // existing failures in logs) — handleFix no longer consumes its return value
  // (Claude does its own ./check test), but it's still called for the side
  // effects.
  it('captures baseline for medium non-CI review fixes', async () => {
    await handleFix(
      'task-fix-baseline-1',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      { context: { scope: 'medium', review_loop: { fix_reason: 'code_review' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.captureTestBaseline)).toHaveBeenCalledTimes(1);
  });

  it('skips baseline for CI-failure fixes so existing PR failures remain in scope', async () => {
    await handleFix(
      'task-fix-baseline-2',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'fix CI failures' },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.captureTestBaseline)).not.toHaveBeenCalled();
    expect(vi.mocked(indexMod.sendLog)).toHaveBeenCalledWith(
      null,
      expect.stringContaining('CI-failure fix'),
    );
  });

  it('runs a targeted CI repro when pytest selectors are available', async () => {
    await handleFix(
      'task-fix-baseline-3',
      {
        branch: 'feature/x',
        base_branch: 'main',
        pr_number: 7,
        prompt: 'CI failed:\nFAILED backend/testing/test_models.py::test_create - AssertionError',
      },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.testFixLoop)).toHaveBeenCalledWith(
      null,
      expect.any(Object),
      undefined,
      undefined,
      expect.any(Object),
      undefined,
      ['backend/app/foo.py'],
      undefined,
      expect.objectContaining({
        targetPytestSelectors: ['backend/testing/test_models.py::test_create'],
        maxFixAttempts: 0,
      }),
    );
  });
});

describe('handleFix — CI failure context is injected into the prompt', () => {
  it('prepends CI repro failureSummary when targeted pytest selector failed locally', async () => {
    vi.mocked(indexMod.testFixLoop).mockResolvedValueOnce({
      passed: false,
      testFailuresDetected: true,
      failureSummary: 'AssertionError at backend/testing/test_models.py::test_create',
    });

    await handleFix(
      'task-fix-ci-repro',
      {
        branch: 'feature/x',
        base_branch: 'main',
        pr_number: 7,
        prompt: 'CI failed:\nFAILED backend/testing/test_models.py::test_create - AssertionError',
      },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Local targeted CI repro');
    expect(prompt).toContain('AssertionError at backend/testing/test_models.py::test_create');
  });
});

describe('handleFix — reviewer working set propagation', () => {
  it("prepends a Reviewer's Working Set section when task.context.investigated_files is present", async () => {
    await handleFix(
      'task-fix-investigated-1',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      {
        context: {
          scope: 'small',
          review_loop: { fix_reason: 'code_review' },
          investigated_files: [
            'admin-ui/src/components/DirectionCard.tsx',
            'admin-ui/src/components/DirectionCard.test.tsx',
          ],
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain("Reviewer's Working Set");
    expect(prompt).toContain('admin-ui/src/components/DirectionCard.tsx');
    expect(prompt).toContain('admin-ui/src/components/DirectionCard.test.tsx');
  });

  it('omits the section entirely when no investigated_files are present', async () => {
    await handleFix(
      'task-fix-investigated-2',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      { context: { scope: 'small', review_loop: { fix_reason: 'code_review' } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain("Reviewer's Working Set");
  });
});

describe('handleFix — stale-premise detection', () => {
  it('pivots prompt to CI-focused guidance when rebase is clean but PR still has failing checks', async () => {
    vi.mocked(execFileSync).mockImplementation(((cmd: string, args?: readonly string[]) => {
      // No conflict markers anywhere.
      if (cmd === 'git' && args && args.includes('grep')) return '';
      if (cmd === 'curl' && args) {
        const url = args[args.length - 1] || '';
        if (url.includes('/pulls/')) {
          return JSON.stringify({ mergeable: true, head: { sha: 'abc123' } });
        }
        if (url.includes('/check-runs')) {
          return JSON.stringify({
            check_runs: [
              { name: 'ci/backend-tests', conclusion: 'failure', html_url: 'https://example/ci1' },
              { name: 'ci/frontend-build', conclusion: 'timed_out', html_url: 'https://example/ci2' },
            ],
          });
        }
      }
      return '';
    }) as unknown as typeof execFileSync);

    await handleFix(
      'task-fix-2',
      {
        branch: 'feature/x', base_branch: 'main', pr_number: 42,
        prompt: 'resolve the merge conflicts on this branch',
      },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Stale-premise notice');
    expect(prompt).toContain('ci/backend-tests');
    expect(prompt).toContain('ci/frontend-build');
    expect(prompt).toContain('Do NOT hunt for conflict markers');
  });

  it('completes with stale-premise status (no Claude run) when rebase is clean and no checks are failing', async () => {
    vi.mocked(execFileSync).mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) return '';
      if (cmd === 'curl' && args) {
        const url = args[args.length - 1] || '';
        if (url.includes('/pulls/')) {
          return JSON.stringify({ mergeable: true, head: { sha: 'def456' } });
        }
        if (url.includes('/check-runs')) {
          return JSON.stringify({ check_runs: [] });
        }
      }
      return '';
    }) as unknown as typeof execFileSync);

    await handleFix(
      'task-fix-3',
      {
        branch: 'feature/x', base_branch: 'main', pr_number: 99,
        prompt: 'resolve the merge conflicts blocking this PR',
      },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // The obsolete path short-circuits before Claude/runCodeChangeTask.
    expect(vi.mocked(runCodeChangeTask)).not.toHaveBeenCalled();

    const statusCalls = vi.mocked(indexMod.persistAndPostStatus).mock.calls;
    expect(statusCalls.length).toBeGreaterThanOrEqual(1);
    const status = statusCalls[0][1];
    expect(status.status).toBe('COMPLETED');
    expect(String(status.logs)).toContain('stale premise');
  });

  it('warns about GitHub-reported unmergeable state when rebase is clean but mergeable=false', async () => {
    vi.mocked(execFileSync).mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) return '';
      if (cmd === 'curl' && args) {
        const url = args[args.length - 1] || '';
        if (url.includes('/pulls/')) {
          return JSON.stringify({ mergeable: false, head: { sha: 'sha0' } });
        }
        if (url.includes('/check-runs')) {
          return JSON.stringify({ check_runs: [] });
        }
      }
      return '';
    }) as unknown as typeof execFileSync);

    await handleFix(
      'task-fix-4',
      {
        branch: 'feature/x', base_branch: 'main', pr_number: 77,
        prompt: 'please rebase and resolve conflict markers',
      },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Stale-premise notice');
    expect(prompt).toContain('GitHub still reports');
    expect(prompt).toContain('mergeable=false');
  });

  it('does not trigger stale-premise path when prompt does not mention conflicts', async () => {
    vi.mocked(execFileSync).mockImplementation(((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) return '';
      // curl should NOT be called in this branch; return empty defensively.
      return '';
    }) as unknown as typeof execFileSync);

    await handleFix(
      'task-fix-5',
      {
        branch: 'feature/x', base_branch: 'main', pr_number: 12,
        prompt: 'tighten the error message wording in the handler',
      },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // No curl invocation means the stale-premise branch was skipped.
    const curlCalls = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'curl');
    expect(curlCalls.length).toBe(0);

    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('Stale-premise notice');
  });
});

// ── handleFix: iteration header ─────────────────────────────────────────────
describe('handleFix — iteration header in enriched prompt', () => {
  it('includes "iteration N of M" header at iteration 1', async () => {
    await handleFix(
      'task-fix-iter1',
      { branch: 'feature/x', base_branch: 'main', pr_number: 50, prompt: 'address review' },
      { context: { scope: 'medium', review_loop: { iteration: 1 } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('iteration 1 of 12');
  });

  it('includes FINAL-attempt warning at the iteration cap', async () => {
    await handleFix(
      'task-fix-final',
      { branch: 'feature/x', base_branch: 'main', pr_number: 50, prompt: 'address review' },
      { context: { scope: 'medium', review_loop: { iteration: 12 } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('FINAL attempt');
  });

  it('triggers Convergence mode at >= 50% of the iteration budget', async () => {
    // medium scope budget = 12; iteration 6 hits the 50% threshold.
    await handleFix(
      'task-fix-converge',
      { branch: 'feature/x', base_branch: 'main', pr_number: 50, prompt: 'address review' },
      { context: { scope: 'medium', review_loop: { iteration: 6 } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Convergence mode');
    expect(prompt).toContain('minimal changes');
  });
});

// ── pushForceWithLeaseRetrying: concurrent push retry paths ─────────────────
//
// pushForceWithLeaseRetrying is exported from review-fix.ts but is no longer
// invoked by handleFix (Claude calls ./pr open which has its own push retry
// logic). The function remains for any caller that wants the push-with-retry
// primitive — we keep its tests so the helper itself is verified.
describe('pushForceWithLeaseRetrying — concurrent push retry', () => {
  it('returns after a successful first push without retrying', async () => {
    vi.mocked(indexMod.gitAsync).mockResolvedValueOnce('');

    await pushForceWithLeaseRetrying('feature/x', null);

    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledWith(['push', '--force-with-lease', 'origin', 'feature/x']);
    // No fetch/rebase on the happy path.
    const gitCalls = vi.mocked(indexMod.git).mock.calls.map(c => c[0]);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'fetch')).toBe(false);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'rebase')).toBe(false);
  });

  it('refetches, rebases, and retries push when first push fails with stale info', async () => {
    vi.mocked(indexMod.gitAsync)
      .mockRejectedValueOnce(new Error('error: failed to push some refs ... stale info'))
      .mockResolvedValueOnce('');
    // git() returns '' for fetch and rebase (both succeed).
    vi.mocked(indexMod.git).mockReturnValue('');

    await pushForceWithLeaseRetrying('feature/x', null);

    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(2);
    const gitCalls = vi.mocked(indexMod.git).mock.calls.map(c => c[0]);
    expect(gitCalls).toContainEqual(['fetch', 'origin', '+refs/heads/feature/x:refs/remotes/origin/feature/x']);
    expect(gitCalls).toContainEqual(['rebase', 'origin/feature/x']);
    // rebase --abort must NOT be invoked when rebase succeeded.
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'rebase' && args[1] === '--abort')).toBe(false);
  });

  it('aborts the rebase and throws a descriptive error when rebase conflicts after stale-info retry', async () => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error('stale info'));
    const abortCalls: string[][] = [];
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (!args) return '';
      if (args[0] === 'rebase' && args[1] !== '--abort') {
        throw new Error('CONFLICT: cannot rebase');
      }
      if (args[0] === 'rebase' && args[1] === '--abort') {
        abortCalls.push(args);
        return '';
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await expect(pushForceWithLeaseRetrying('feature/x', null))
      .rejects.toThrow(/Rebase onto updated origin\/feature\/x conflicted after concurrent push/);

    expect(abortCalls.length).toBe(1);
    // Second push must NOT be attempted when rebase conflicted.
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(1);
  });

  it('propagates a non-stale push error immediately without retrying', async () => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error('remote: Permission denied to branch'));

    await expect(pushForceWithLeaseRetrying('feature/x', null))
      .rejects.toThrow(/Permission denied/);

    // Only the first push; no fetch, no rebase, no retry push.
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(1);
    const gitCalls = vi.mocked(indexMod.git).mock.calls.map(c => c[0]);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'fetch')).toBe(false);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'rebase')).toBe(false);
  });

  it('silently ignores a rebase --abort failure and still throws the descriptive conflict error', async () => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error('stale info'));
    vi.mocked(indexMod.git).mockImplementation(((args?: string[]) => {
      if (!args) return '';
      if (args[0] === 'rebase' && args[1] !== '--abort') {
        throw new Error('CONFLICT during rebase');
      }
      if (args[0] === 'rebase' && args[1] === '--abort') {
        throw new Error('fatal: no rebase in progress');
      }
      return '';
    }) as unknown as typeof indexMod.git);

    await expect(pushForceWithLeaseRetrying('feature/x', null))
      .rejects.toThrow(/Rebase onto updated origin\/feature\/x conflicted after concurrent push/);
  });

  it('retries multiple times when successive pushes return stale info (default maxRetries=2)', async () => {
    vi.useFakeTimers();
    vi.mocked(indexMod.gitAsync)
      .mockRejectedValueOnce(new Error('stale info'))  // initial push
      .mockRejectedValueOnce(new Error('stale info'))  // retry 1
      .mockResolvedValueOnce('');                       // retry 2 succeeds
    vi.mocked(indexMod.git).mockReturnValue('');

    const promise = pushForceWithLeaseRetrying('feature/x', null);
    await vi.runAllTimersAsync();
    await promise;
    vi.useRealTimers();

    // 1 initial + 2 retries = 3 total push attempts.
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(3);
    // Logged attempt numbers for observability.
    const logs = vi.mocked(indexMod.sendLog).mock.calls.map(c => c[1]).join('\n');
    expect(logs).toContain('Retry attempt 1/2');
    expect(logs).toContain('Retry attempt 2/2');
  });

  it('throws the last stale-info error when all retries are exhausted', async () => {
    vi.useFakeTimers();
    vi.mocked(indexMod.gitAsync).mockRejectedValue(new Error('stale info again'));
    vi.mocked(indexMod.git).mockReturnValue('');

    const promise = pushForceWithLeaseRetrying('feature/x', null);
    const assertion = expect(promise).rejects.toThrow(/stale info/);
    await vi.runAllTimersAsync();
    await assertion;
    vi.useRealTimers();

    // 1 initial + 2 retries (default) = 3 total push attempts.
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(3);
  });

  it('respects a custom maxRetries value (0 means no retries)', async () => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error('stale info'));
    vi.mocked(indexMod.git).mockReturnValue('');

    await expect(pushForceWithLeaseRetrying('feature/x', null, 0))
      .rejects.toThrow(/stale info/);

    // Only the initial push; no fetch/rebase/retry push.
    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(1);
    const gitCalls = vi.mocked(indexMod.git).mock.calls.map(c => c[0]);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'fetch')).toBe(false);
  });
});

describe('detectMigrationConflict', () => {
  it('returns detected=false when PR has no migration files', () => {
    const result = detectMigrationConflict(
      ['backend/app/main.py', 'agent/src/handlers/review-fix.ts'],
      ['backend/alembic/versions/0050_x.py'],
    );
    expect(result.detected).toBe(false);
    expect(result.prMigrations).toEqual([]);
  });

  it('returns detected=false when PR migration is the newest (no conflicting base migrations)', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py'],
      ['backend/alembic/versions/0049_prior.py', 'backend/alembic/versions/0050_add_foo.py'],
    );
    expect(result.detected).toBe(false);
    expect(result.prMigrations).toEqual(['backend/alembic/versions/0050_add_foo.py']);
  });

  it('detects silent conflict when base has a newer migration with equal-or-higher revision', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py'],
      [
        'backend/alembic/versions/0049_prior.py',
        'backend/alembic/versions/0050_merged_first.py',
        'backend/alembic/versions/0051_also_merged.py',
      ],
    );
    expect(result.detected).toBe(true);
    expect(result.baseNewMigrations).toEqual([
      'backend/alembic/versions/0050_merged_first.py',
      'backend/alembic/versions/0051_also_merged.py',
    ]);
  });

  it('ignores non-migration files and __init__.py', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py', 'backend/alembic/versions/__init__.py'],
      ['backend/alembic/versions/__init__.py', 'backend/alembic/versions/0050_other.py'],
    );
    expect(result.detected).toBe(true);
    expect(result.prMigrations).toEqual(['backend/alembic/versions/0050_add_foo.py']);
  });
});

describe('buildBlueprintPrFooter', () => {
  // Note: 2026-05-04 — the in-handler call site for this footer
  // (patchPrBodyMeta) was deleted along with the wrapper-driven push pipeline.
  // The function itself is still exported and remains useful as a composable
  // helper for any caller that wants to render the footer (e.g. ./pr open in
  // the future, or ad-hoc PR body annotation). Keep its unit tests.
  it('returns empty string when no meaningful data', () => {
    expect(buildBlueprintPrFooter({})).toBe('');
  });

  it('returns empty string for iterations=1 (single pass is not noteworthy)', () => {
    expect(buildBlueprintPrFooter({ iterations: 1 })).toBe('');
  });

  it('includes iteration count when > 1', () => {
    const result = buildBlueprintPrFooter({ iterations: 3 });
    expect(result).toContain('Review iteration 3');
    expect(result).toContain('<!-- blueprint:meta -->');
  });

  it('includes max iterations in label when provided', () => {
    const result = buildBlueprintPrFooter({ iterations: 3, maxIterations: 10 });
    expect(result).toContain('Review iteration 3 of 10');
    expect(result).toContain('<!-- blueprint:meta -->');
  });

  it('includes direction ID alone when no title provided', () => {
    const result = buildBlueprintPrFooter({ directionId: 'd-abc123' });
    expect(result).toContain('direction: d-abc123');
    expect(result).not.toContain('undefined');
  });

  it('includes direction title with ID when both present', () => {
    const result = buildBlueprintPrFooter({ directionId: 'd-abc', directionTitle: 'PR clarity' });
    expect(result).toContain('PR clarity (d-abc)');
  });

  it('truncates long direction titles to 60 characters', () => {
    const longTitle = 'x'.repeat(80);
    const result = buildBlueprintPrFooter({ directionId: 'd-x', directionTitle: longTitle });
    expect(result).toContain('x'.repeat(60));
    expect(result).not.toContain('x'.repeat(61));
  });

  it('combines iterations and direction in a single footer line', () => {
    const result = buildBlueprintPrFooter({ iterations: 2, maxIterations: 12, directionId: 'd-abc', directionTitle: 'PR clarity' });
    expect(result).toMatch(/Review iteration 2 of 12 · direction: PR clarity \(d-abc\)/);
    expect(result).toContain('<!-- blueprint:meta -->');
  });

  it('includes provider when set', () => {
    const result = buildBlueprintPrFooter({ provider: 'anthropic' });
    expect(result).toContain('provider: anthropic');
    expect(result).toContain('<!-- blueprint:meta -->');
  });

  it('includes provider and model when both set', () => {
    const result = buildBlueprintPrFooter({ provider: 'anthropic', model: 'claude-3-5-sonnet-20241022' });
    expect(result).toContain('provider: anthropic (claude-3-5-sonnet-20241022)');
  });

  it('shows unknown provider when only model is set', () => {
    const result = buildBlueprintPrFooter({ model: 'claude-3-5-sonnet-20241022' });
    expect(result).toContain('provider: unknown (claude-3-5-sonnet-20241022)');
  });
});

describe('buildMigrationConflictGuidance', () => {
  it('returns empty string when no conflict detected', () => {
    expect(buildMigrationConflictGuidance({ detected: false, prMigrations: [], baseNewMigrations: [] })).toBe('');
  });

  it('mentions renumbering, down_revision chaining, and EXPECTED_SCHEMA_REVISION bump', () => {
    const out = buildMigrationConflictGuidance({
      detected: true,
      prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
      baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
    });
    expect(out).toMatch(/Migration revision conflict/);
    expect(out).toMatch(/down_revision/);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION/);
    expect(out).toMatch(/0051_merged\.py/);
  });

  it('includes computed next revision number, renamed file, and EXPECTED_SCHEMA_REVISION value', () => {
    const out = buildMigrationConflictGuidance({
      detected: true,
      prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
      baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
    });
    expect(out).toMatch(/revision="0051"/);
    expect(out).toMatch(/backend\/alembic\/versions\/0052_add_foo\.py/);
    expect(out).toMatch(/revision = "0052"/);
    expect(out).toMatch(/down_revision = "0051"/);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION = "0052"/);
  });
});

// ── formatTrajectorySection ───────────────────────────────────────────────────
describe('formatTrajectorySection — fix-prompt iteration arc renderer', () => {
  it('returns empty when history is empty', () => {
    expect(formatTrajectorySection([])).toBe('');
  });

  it('returns empty when no entries carry verdict info (older backends)', () => {
    const out = formatTrajectorySection([
      { iteration: 1, feedback: 'foo' },
      { iteration: 2, feedback: 'bar' },
    ]);
    expect(out).toBe('');
  });

  it('renders APPROVE and REQUEST_CHANGES entries with diff signature', () => {
    const out = formatTrajectorySection([
      { iteration: 1, feedback: '', verdict: 'APPROVE', changed_files: ['a.py', 'b.py'] },
      { iteration: 2, feedback: 'review feedback', verdict: 'REQUEST_CHANGES', changed_files: ['main.py'] },
    ]);
    expect(out).toContain('## PR trajectory');
    expect(out).toContain('iter 1: APPROVE');
    expect(out).toContain('iter 2: REQUEST_CHANGES');
    expect(out).toContain('a.py, b.py');
    expect(out).toContain('main.py');
  });

  it('truncates long file lists with a +N more suffix', () => {
    const manyFiles = Array.from({ length: 12 }, (_, i) => `f${i}.py`);
    const out = formatTrajectorySection([
      { iteration: 1, feedback: '', verdict: 'APPROVE', changed_files: manyFiles },
    ]);
    expect(out).toMatch(/f0\.py.*f7\.py.*\+4 more/s);
  });
});

// ── formatDriftSection ────────────────────────────────────────────────────────
describe('formatDriftSection — drift signal renderer', () => {
  it('returns empty when no drift', () => {
    expect(formatDriftSection({ drift_in: [], drift_out: [] })).toBe('');
    expect(formatDriftSection(undefined)).toBe('');
  });

  it('renders drift_in with first_appeared + flag_count metadata', () => {
    const out = formatDriftSection({
      drift_in: [
        { file: 'main.py', first_appeared_iteration: 2, flag_count: 3 },
        { file: 'other.py' }, // bare entry — no metadata
      ],
      drift_out: ['test_reaper.py'],
    });
    expect(out).toContain('main.py');
    expect(out).toContain('first appeared iter 2');
    expect(out).toContain('flagged 3x');
    // Bare entry should not show metadata.
    expect(out).toMatch(/- other\.py(?!.*first appeared)/s);
    expect(out).toContain('test_reaper.py');
  });

  it('frames drift as informational with explicit "scope expansion is sometimes correct" guidance', () => {
    const out = formatDriftSection({
      drift_in: [{ file: 'foo.py' }],
      drift_out: [],
    });
    // Must NOT be directive — agent should retain judgment.
    expect(out).toContain('informational — not a directive');
    // Must explicitly acknowledge justified scope expansion (the user-flagged
    // concern that drift signaling shouldn't push the agent to reflexively revert).
    expect(out).toContain('scope expansion IS sometimes the right call');
    expect(out).toContain('caller-impact');
    expect(out).toContain('discovered-dep');
    expect(out).toContain('cluster-boundary correction');
  });
});

// ── verifyStepsOk ───────────────────────────────────────────────────────────
describe('verifyStepsOk — sycophancy guard for fix_agent_responses', () => {
  it('rejects entries with no verify steps', () => {
    expect(verifyStepsOk({
      claim: 'this is fine',
      verify: [],
    })).toBe(false);
  });

  it('rejects entries where every verify step is non-mechanical (sycophancy patterns)', () => {
    expect(verifyStepsOk({
      claim: 'I disagree',
      verify: ['trust me, I tested it', "I'm confident this works"],
    })).toBe(false);
  });

  it('accepts entries with at least one mechanical verify step even if other steps are non-mechanical', () => {
    expect(verifyStepsOk({
      claim: 'caller-impact justification',
      verify: [
        'I re-read the code',  // non-mechanical
        'grep -n "newApi" backend/app/auth.py',  // mechanical
      ],
    })).toBe(true);
  });

  it('accepts purely mechanical verify steps', () => {
    expect(verifyStepsOk({
      claim: 'fix is in helper file',
      verify: ['cat backend/app/helpers.py | head -20', 'pytest backend/testing/test_helpers.py::test_X'],
    })).toBe(true);
  });

  it('rejects entries with empty claim', () => {
    expect(verifyStepsOk({
      claim: '',
      verify: ['grep "foo" file.py'],
    })).toBe(false);
  });
});

// ── Structural prompt invariants (regression-detection) ───────────────────────
// Pins the cross-cutting guidance blocks that handleFix (code-change) and
// handleReview (read-only) MUST / MUST NOT assemble. A future PR that drops a
// block or incorrectly adds CODE_CHANGE_HELPER_GUIDANCE to handleReview will
// fail here before merging.
describe('handleFix structural prompt invariants', () => {
  const FIX_PAYLOAD = {
    branch: 'fix-branch',
    base_branch: 'main',
    pr_number: 42,
    prompt: 'Fix the issue described in the review.',
  };
  const FIX_TASK = { title: 'Fix', description: '', context: {}, project_context: {} };

  beforeEach(() => { vi.clearAllMocks(); });

  it('handleFix prompt includes all required code-change guidance blocks', async () => {
    await handleFix('task-fix', FIX_PAYLOAD, FIX_TASK, 'token', 'owner', 'repo', null, statusRef());
    const p = capturedFixPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');       // CODE_CHANGE_HELPER_GUIDANCE
    expect(p).toContain('./playbook set');                                // PLAYBOOK_GUIDANCE
    expect(p).toContain('Read The Failure Message Literally');            // READ_THE_FAILURE_MESSAGE
    expect(p).toContain('Size Guardrails Are Advisory');                  // SIZE_GUARDRAIL_GUIDANCE
    expect(p).toContain("If You're Retrying");                           // PRIOR_ATTEMPT_DIVERGENCE
    expect(p).toContain('When You Find Bigger Problems Than Your Task');  // BEYOND_SCOPE_REPORTING
    expect(p).toContain('Use Grep/Glob for code search');                // DEFAULT_TOOL_GUIDANCE
  });
});

describe('handleFix task-type-specific self-review focus blocks', () => {
  const basePayload = {
    branch: 'fix-branch',
    base_branch: 'main',
    pr_number: 42,
    prompt: 'Fix the issue.',
  };
  const baseTask = { title: 'Fix', description: '', context: {}, project_context: {} };

  beforeEach(() => { vi.clearAllMocks(); });

  it('includes migrate focus block for migrate original_task_type', async () => {
    await handleFix(
      'task-migrate-fix',
      { ...basePayload, original_task_type: 'migrate' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: migrate tasks');
    expect(p).toContain('down_revision');
    expect(p).toContain('Task lifecycle — you own it end-to-end'); // generic guidance still present
  });

  it('includes refactor focus block for refactor original_task_type', async () => {
    await handleFix(
      'task-refactor-fix',
      { ...basePayload, original_task_type: 'refactor' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: refactor tasks');
    expect(p).toContain('NO new behavior');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes qa focus block for qa original_task_type', async () => {
    await handleFix(
      'task-qa-fix',
      { ...basePayload, original_task_type: 'qa' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: qa tasks');
    expect(p).toContain('Behavior enumeration');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes create focus block for create original_task_type', async () => {
    await handleFix(
      'task-create-fix',
      { ...basePayload, original_task_type: 'create' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: create tasks');
    expect(p).toContain('Test coverage');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes fix focus block for fix original_task_type', async () => {
    await handleFix(
      'task-fix-fix',
      { ...basePayload, original_task_type: 'fix' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: fix tasks');
    expect(p).toContain('Regression surface');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes docs focus block for docs original_task_type', async () => {
    await handleFix(
      'task-docs-fix',
      { ...basePayload, original_task_type: 'docs' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: docs tasks');
    expect(p).toContain('Link rot');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('includes deps focus block for deps original_task_type', async () => {
    await handleFix(
      'task-deps-fix',
      { ...basePayload, original_task_type: 'deps' },
      baseTask,
      'token', 'owner', 'repo', null, statusRef(),
    );
    const p = capturedFixPrompt();
    expect(p).toContain('Self-review focus: deps tasks');
    expect(p).toContain('Lockfile');
    expect(p).toContain('Task lifecycle — you own it end-to-end');
  });

  it('does not include a type-specific block for unknown task types', async () => {
    for (const taskType of ['unknown', '']) {
      vi.clearAllMocks();
      await handleFix(
        `task-${taskType}-fix`,
        { ...basePayload, original_task_type: taskType },
        baseTask,
        'token', 'owner', 'repo', null, statusRef(),
      );
      const p = capturedFixPrompt();
      expect(p).not.toContain('Self-review focus: migrate tasks');
      expect(p).not.toContain('Self-review focus: refactor tasks');
      expect(p).not.toContain('Self-review focus: qa tasks');
      expect(p).not.toContain('Self-review focus: create tasks');
      expect(p).not.toContain('Self-review focus: fix tasks');
      expect(p).not.toContain('Self-review focus: docs tasks');
      expect(p).not.toContain('Self-review focus: deps tasks');
      expect(p).toContain('Task lifecycle — you own it end-to-end'); // generic always present
    }
  });
});

describe('handleReview structural prompt invariants', () => {
  const REVIEW_PAYLOAD = { pr_number: 1, branch: 'feat', base_branch: 'main' };
  const REVIEW_TASK = { title: 'Review', description: '', context: {}, project_context: {} };

  beforeEach(() => { vi.clearAllMocks(); });

  async function captureReviewPrompt(): Promise<string> {
    await handleReview('task-rev', REVIEW_PAYLOAD, REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef());
    const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    return calls[calls.length - 1][0];
  }

  it('handleReview prompt includes DEFAULT_TOOL_GUIDANCE', async () => {
    expect(await captureReviewPrompt()).toContain('Use Grep/Glob for code search');
  });

  it('handleReview prompt does NOT include CODE_CHANGE_HELPER_GUIDANCE', async () => {
    expect(await captureReviewPrompt()).not.toContain('Task lifecycle — you own it end-to-end');
  });

  it('handleReview prompt contains task-type fingerprint: no informational-only verdict path', async () => {
    expect(await captureReviewPrompt()).toContain('There is no informational-only path');
  });

  it('handleReview prompt contains APPROVE verdict marker', async () => {
    expect(await captureReviewPrompt()).toContain('"verdict": "APPROVE"');
  });

  it('handleReview prompt contains REQUEST_CHANGES verdict marker', async () => {
    expect(await captureReviewPrompt()).toContain('"verdict": "REQUEST_CHANGES"');
  });

  it('handleReview prompt contains no-third-verdict guidance', async () => {
    expect(await captureReviewPrompt()).toContain('Do NOT emit "COMMENT", "NEUTRAL", "ADVISORY"');
  });

  it('handleReview prompt contains Severity Classification Guide', async () => {
    expect(await captureReviewPrompt()).toContain('**Severity Classification Guide:**');
  });

  it('handleReview prompt contains verdict JSON file path pattern', async () => {
    expect(await captureReviewPrompt()).toContain('/tmp/review_verdict_');
  });

  it('handleReview prompt contains Correctness & Logic checklist section', async () => {
    expect(await captureReviewPrompt()).toContain('**Correctness & Logic:**');
  });

  it('handleReview prompt contains Security checklist section', async () => {
    expect(await captureReviewPrompt()).toContain('**Security:**');
  });
});

describe('handleReview prompt determinism', () => {
  const REVIEW_PAYLOAD = { pr_number: 1, branch: 'feat', base_branch: 'main' };
  const REVIEW_TASK = { title: 'Review', description: '', context: {}, project_context: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('produces identical prompts for identical inputs (no Date.now/Math.random leaks)', async () => {
    const invoke = async () => {
      vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
      await handleReview('task-rev', REVIEW_PAYLOAD, REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef());
      const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
      return calls[calls.length - 1][0];
    };
    const prompt1 = await invoke();
    vi.clearAllMocks();
    const prompt2 = await invoke();
    expect(prompt1).toBe(prompt2);
  });
});
