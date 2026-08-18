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
    normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
    strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
    resolveBaseBranch: (payload: Record<string, unknown>, task: Record<string, unknown>, fallback = 'main') => {
      if (typeof payload.base_branch === 'string' && payload.base_branch) return payload.base_branch;
      const meta = task.project_metadata as Record<string, unknown> | undefined;
      if (typeof meta?.integration_branch === 'string' && meta.integration_branch) return meta.integration_branch;
      return fallback;
    },
    sanitize: (s: string) => s,
    WORKSPACE: '/tmp/workspace',
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
    setupWorkspace: vi.fn(),
    runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
    extractPytestSelectorsFromOutput: vi.fn((output: string) => {
      const match = output.match(/FAILED\s+(backend\/testing\/\S+\.py::\S+)/);
      return match ? [match[1].replace(/\s+-.*$/, '')] : [];
    }),
    snapshotToolTrace: vi.fn().mockReturnValue([]),
    extractInvestigatedFiles: vi.fn().mockReturnValue([]),
    cloneWithRetry: vi.fn(),
    sendHeartbeatSync: vi.fn(),
    setCurrentPromptFingerprint: vi.fn(),
}));

// The test verification loop now lives in utils/test-fix-loop.ts; handlers import
// captureTestBaseline and testFixLoop from there.
vi.mock('../utils/test-fix-loop.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-fix-loop.js')>();
  return {
    ...actual,
    captureTestBaseline: vi.fn().mockResolvedValue(null),
    testFixLoop: vi.fn().mockResolvedValue({ passed: true, testFailuresDetected: false, failureSummary: '' }),
  };
});

vi.mock('../workspacePrep.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../workspacePrep.js')>();
  return { ...actual, prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined) };
});

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

vi.mock('../build-config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../build-config.js')>();
  return { ...actual, resolveBuildConfig: vi.fn().mockReturnValue({}) };
});

// Mock the code-change module so handleFix tests can assert on what was
// passed to runCodeChangeTask without exercising marker-detection branches.
vi.mock('./code-change.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./code-change.js')>();
  return { ...actual, runCodeChangeTask: vi.fn().mockResolvedValue(undefined) };
});

// Spy on the read-only ./fetch writer while keeping buildFetchGuidance and the
// PLAN_FETCH_VERBS constant real, so prompt-guidance assertions exercise the
// genuine text and the writer-invocation assertions can inspect the call args.
vi.mock('./strategic-helpers.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./strategic-helpers.js')>();
  return { ...actual, writeFetchHelper: vi.fn() };
});

vi.mock('../utils/viewport-shot-download.js', () => ({
  downloadViewportShots: vi.fn().mockReturnValue([]),
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
import * as testLoopMod from '../utils/test-fix-loop.js';
import { expectCalledWith, expectLastCalledWith } from '../utils/typed-mock-call.js';
import {
  handleReview, handleFix,
  detectMediumScopeSubsystemHint, pushForceWithLeaseRetrying,
  buildBlueprintPrFooter, formatLaneResumeSection,
  REVIEW_VERDICT_JSON_MAX_LENGTH, serializeBoundedReviewVerdict,
} from './review-fix.js';
import { computeReviewPromptFingerprint } from '../prompts/review-prompt.js';
import { runCodeChangeTask } from './code-change.js';
import { resolveBuildConfig } from '../build-config.js';
import * as promptMod from '../prompt.js';
import { shouldFanOutReview, shouldFanOutFix } from '../constants.js';
import { computePromptFingerprint, buildScopedLintOneliner, READ_ONLY_BASH_WRITE_NOTE } from '../prompt.js';
import { writeFetchHelper, PLAN_FETCH_VERBS } from './strategic-helpers.js';
import { getFormatComplianceTelemetry, resetFormatComplianceTelemetry } from '../telemetry.js';
import * as viewportShotMod from '../utils/viewport-shot-download.js';

const BLUEPRINT_CONFIG = {
  directories: [
    { path: 'agent', type: 'node' },
    { path: 'admin-ui', type: 'node' },
    { path: 'backend', type: 'python' },
  ],
};

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
  resetFormatComplianceTelemetry();
  vi.mocked(fs.existsSync).mockReturnValue(false);
  vi.mocked(fs.readFileSync).mockReturnValue('');
  // Default: ls-remote returns a ref line (branch exists) so handleReview tests
  // that don't care about the deleted-branch path proceed normally. Tests that
  // need the branch-missing or transport-error behavior override this per-test.
  vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
    if (cmd === 'git' && Array.isArray(args) && args.includes('ls-remote')) {
      return 'abc123\trefs/heads/feature\n';
    }
    return '';
  });
  vi.mocked(indexMod.git).mockReturnValue('');
  vi.mocked(indexMod.gitAsync).mockResolvedValue('');
  vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  vi.mocked(indexMod.persistAndPostStatus).mockResolvedValue(undefined);
  vi.mocked(indexMod.apiGet).mockResolvedValue({ data: { token: 'fresh' } });
  vi.mocked(testLoopMod.testFixLoop).mockResolvedValue({ passed: true, testFailuresDetected: false, failureSummary: '' });
  vi.mocked(testLoopMod.captureTestBaseline).mockResolvedValue(null);
  // fetch is used by handleReview for PR metadata
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ title: 'PR title', body: 'PR body' }),
  });
});

// ── Authoritative blocker prompt ────────────────────────────────────────────
describe('serializeBoundedReviewVerdict', () => {
  it('preserves an exact-boundary verdict byte-for-byte', () => {
    const verdict = { verdict: 'APPROVE', feedback: '' };
    const envelopeLength = JSON.stringify(verdict).length;
    verdict.feedback = 'x'.repeat(REVIEW_VERDICT_JSON_MAX_LENGTH - envelopeLength);
    const original = JSON.stringify(verdict);

    expect(original).toHaveLength(REVIEW_VERDICT_JSON_MAX_LENGTH);
    expect(serializeBoundedReviewVerdict(verdict)).toBe(original);
  });

  it('returns valid bounded JSON and preserves routing fields one character over the boundary', () => {
    const verdict = { verdict: 'REQUEST_CHANGES', feedback: '' };
    const envelopeLength = JSON.stringify(verdict).length;
    verdict.feedback = 'x'.repeat(REVIEW_VERDICT_JSON_MAX_LENGTH - envelopeLength + 1);

    const serialized = serializeBoundedReviewVerdict(verdict);
    const parsed = JSON.parse(serialized) as Record<string, unknown>;

    expect(serialized.length).toBeLessThanOrEqual(REVIEW_VERDICT_JSON_MAX_LENGTH);
    expect(parsed.verdict).toBe('REQUEST_CHANGES');
    expect(parsed.verdict_json_truncated).toBe(true);
    expect(String(parsed.feedback)).toMatch(/…$/);
  });

  it('keeps issue identity, locations, and reviewed-SHA provenance while compacting verbose text', () => {
    const serialized = serializeBoundedReviewVerdict({
      verdict: 'REQUEST_CHANGES',
      feedback: 'f'.repeat(20_000),
      issues: [{
        issue_id: 'BP-123',
        severity: 'major',
        description: 'd'.repeat(20_000),
        file: 'agent/src/handlers/review-fix.ts',
        line: 960,
      }],
      reviewed_head_sha: 'a'.repeat(40),
      reviewed_base_sha: 'b'.repeat(40),
    });
    const parsed = JSON.parse(serialized) as {
      verdict: string;
      issues: Array<Record<string, unknown>>;
      reviewed_head_sha: string;
      reviewed_base_sha: string;
      verdict_json_truncated: boolean;
    };

    expect(serialized.length).toBeLessThanOrEqual(REVIEW_VERDICT_JSON_MAX_LENGTH);
    expect(parsed.verdict).toBe('REQUEST_CHANGES');
    expect(parsed.issues[0]).toMatchObject({
      issue_id: 'BP-123',
      severity: 'major',
      file: 'agent/src/handlers/review-fix.ts',
      line: 960,
    });
    expect(parsed.reviewed_head_sha).toBe('a'.repeat(40));
    expect(parsed.reviewed_base_sha).toBe('b'.repeat(40));
    expect(parsed.verdict_json_truncated).toBe(true);
  });

  it('bounds pathological issue arrays without emitting truncated JSON', () => {
    const serialized = serializeBoundedReviewVerdict({
      verdict: 'REQUEST_CHANGES',
      feedback: 'large review',
      issues: Array.from({ length: 500 }, (_, index) => ({
        issue_id: `BP-${index}`,
        severity: 'major',
        description: `finding ${index} ${'x'.repeat(400)}`,
        file: `agent/src/file-${index}.ts`,
        line: index + 1,
      })),
    });
    const parsed = JSON.parse(serialized) as {
      issues: Array<Record<string, unknown>>;
      verdict_json_truncated: boolean;
    };

    expect(serialized.length).toBeLessThanOrEqual(REVIEW_VERDICT_JSON_MAX_LENGTH);
    expect(parsed.issues.length).toBeGreaterThan(0);
    expect(parsed.issues.length).toBeLessThanOrEqual(50);
    expect(parsed.verdict_json_truncated).toBe(true);
  });
});

describe('authoritative blocker prompt', () => {
  const histWithStructured = [{
    iteration: 1,
    feedback: 'historical detail that must not be duplicated into the prompt',
    issues: [{ severity: 'major', description: 'real problem', file: 'a.ts', line: 10 }],
  }];

  it('reviewer receives role-appropriate authoritative-ledger instructions', async () => {
    await handleReview(
      'task-authoritative-review',
      { pr_number: 61, branch: 'feat/z', base_branch: 'main', original_prompt: '' },
      {
        context: {
          scope: 'medium',
          review_loop: {
            iteration: 2,
            review_history: histWithStructured,
            issue_ledger: {
              open: [{ issue_id: 'BP-review', severity: 'major', description: 'verify the null guard' }],
            },
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('[MAJOR] [BP-review] verify the null guard');
    expect(prompt).toContain('Re-raise an unresolved blocker with its exact BP-... id');
    expect(prompt).not.toContain('fix agents cannot close blockers');
    expect(prompt).not.toContain('historical detail that must not be duplicated');
    expect(prompt).not.toContain('[MAJOR] [a.ts:10] real problem');
  });
});

// -- handleFix: operator retry guidance ---------------------------------------
describe('handleFix - operator retry guidance', () => {
  it('renders prompt_suffix after a stale fix prompt', async () => {
    const stalePrompt = 'Renumber the migration to 0626.';
    const guidance = 'Do not create 0626. Remove the duplicate migration instead.';
    await handleFix(
      'task-requeued-fix',
      {
        branch: 'feat/migration-fix',
        base_branch: 'automain',
        pr_number: 15420,
        prompt: stalePrompt,
        prompt_suffix: `## Operator retry guidance\n${guidance}`,
      },
      {
        context: {
          scope: 'small',
          review_loop: { iteration: 1, review_history: [] },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain(stalePrompt);
    expect(prompt).toContain('Additional instructions:');
    expect(prompt).toContain(guidance);
    expect(prompt.lastIndexOf(guidance)).toBeGreaterThan(prompt.lastIndexOf(stalePrompt));
  });
});
// -- handleFix: operator steering reaches the fix prompt ----------------------

describe('handleFix — operator steering (strategic_intent)', () => {
  const histWithStructured = [{
    iteration: 1,
    feedback: 'address the issues',
    issues: [{ severity: 'major', description: 'real problem', file: 'a.ts', line: 10 }],
  }];

  it('prepends ## Project Vision before ## Operator Steering when vision is present', async () => {
    await handleFix(
      'task-vision-fix',
      { branch: 'feat/s', base_branch: 'main', pr_number: 7, prompt: 'address review' },
      {
        vision: {
          purpose: 'Ship the right user outcome.',
          success_criteria: ['The fix is validated by the review feedback.'],
          priorities: ['Keep the change minimal.'],
          context: 'Fixes should inherit the project-level why.',
        },
        context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithStructured } },
        strategic_intent: {
          purpose: 'Build an autonomous coding studio.',
          current_focus: 'Thread operator steering into tactical prompts.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Project Vision');
    expect(prompt).toContain('**Purpose:** Ship the right user outcome.');
    expect(prompt).toContain('**Success criteria:**');
    expect(prompt).toContain('- The fix is validated by the review feedback.');
    expect(prompt).toContain('**Priorities:**');
    expect(prompt).toContain('- Keep the change minimal.');
    expect(prompt).toContain('**Context:** Fixes should inherit the project-level why.');
    expect(prompt).toContain('## Operator Steering');
    expect(prompt).toContain('**Current focus:** Thread operator steering into tactical prompts.');
    expect(prompt).toContain('**Purpose:** Build an autonomous coding studio.');
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });

  it('prepends ## Operator Steering alongside the existing review context', async () => {
    await handleFix(
      'task-steering-fix',
      { branch: 'feat/s', base_branch: 'main', pr_number: 7, prompt: 'address review' },
      {
        context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithStructured } },
        strategic_intent: {
          purpose: 'Build an autonomous coding studio.',
          current_focus: 'Thread operator steering into tactical prompts.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Operator Steering');
    expect(prompt).toContain('**Current focus:** Thread operator steering into tactical prompts.');
    expect(prompt).toContain('**Purpose:** Build an autonomous coding studio.');
    // The steering section is additive — current review feedback remains present.
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });

  it('omits ## Operator Steering when strategic_intent is absent', async () => {
    await handleFix(
      'task-no-steering-fix',
      { branch: 'feat/s', base_branch: 'main', pr_number: 8, prompt: 'address review' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithStructured } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('## Operator Steering');
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });
});

// ── handleFix: repo overview ─────────────────────────────────────────────────
describe('handleFix — repo overview (repo_overview)', () => {
  const histWithIssue = [{
    iteration: 1,
    feedback: 'address the issues',
    issues: [{ severity: 'major', description: 'fix this', file: 'a.ts', line: 5 }],
  }];

  it('prepends ## Repo Overview into the fix prompt when repo_overview is present', async () => {
    await handleFix(
      'task-fix-repo-overview',
      { branch: 'feat/r', base_branch: 'main', pr_number: 9, prompt: 'address review' },
      {
        context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithIssue } },
        repo_overview: {
          tech_stack: 'Python, FastAPI, React',
          directory_layout: 'backend/ agent/ admin-ui/',
          first_steps_summary: 'pip install && npm install',
          bootstrap_completed_at: '2026-01-15T10:00:00Z',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Repo Overview');
    expect(prompt).toContain('**Tech stack:** Python, FastAPI, React');
    expect(prompt).toContain('**Directory layout:** backend/ agent/ admin-ui/');
    expect(prompt).toContain('**First steps:** pip install && npm install');
    expect(prompt).toContain('**Bootstrapped:** 2026-01-15T10:00:00Z');
    // Current review feedback remains present.
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });

  it('omits ## Repo Overview from the fix prompt when repo_overview is absent', async () => {
    await handleFix(
      'task-fix-no-repo-overview',
      { branch: 'feat/r', base_branch: 'main', pr_number: 10, prompt: 'address review' },
      {
        context: { scope: 'small', review_loop: { iteration: 2, review_history: histWithIssue } },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('## Repo Overview');
  });
});

// ── handleFix: stale-premise / provenance notice ───────────────────────────
describe('handleFix — provenance / stale-premise notice', () => {
  const hist = [{
    iteration: 1,
    feedback: 'address the issues',
    issues: [{ severity: 'major', description: 'real problem', file: 'a.ts', line: 10 }],
  }];

  it('renders the stale-premise notice when _provenance_warnings is present', async () => {
    await handleFix(
      'task-provenance-fix',
      { branch: 'feat/s', base_branch: 'main', pr_number: 9, prompt: 'address review' },
      {
        context: {
          scope: 'small',
          review_loop: { iteration: 2, review_history: hist },
          _provenance_warnings: {
            cited_files_changed_since_generation: true,
            broken_source_ref_count: 2,
            broken_refs: ['app/foo.py:5', 'app/bar.ts'],
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Stale-premise notice');
    expect(prompt).toContain('Cited files changed since the finding was generated');
    expect(prompt).toContain('2 broken/missing citation(s)');
    expect(prompt).toContain('app/foo.py:5');
    expect(prompt).toContain('REPRODUCE-FIRST');
    // Additive — current review feedback remains present.
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });

  it('omits the stale-premise notice when _provenance_warnings is absent', async () => {
    await handleFix(
      'task-no-provenance-fix',
      { branch: 'feat/s', base_branch: 'main', pr_number: 10, prompt: 'address review' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: hist } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).not.toContain('## Stale-premise notice');
  });
});

// ── handleFix: lane-resume section (review-fix improvement-lane pilot) ───────
describe('handleFix — lane resume section', () => {
  const hist = [{
    iteration: 1,
    feedback: 'address the issues',
    issues: [{ severity: 'major', description: 'real problem', file: 'a.ts', line: 10 }],
  }];

  it('renders the ## Lane Resume section when context.lane_resume is present', async () => {
    await handleFix(
      'task-lane-resume-fix',
      { branch: 'lane/x', base_branch: 'main', pr_number: 11, prompt: 'address review' },
      {
        context: {
          scope: 'small',
          review_loop: { iteration: 2, review_history: hist },
          lane_resume: {
            checkpoint_seq: 3,
            last_checkpoint_sha: 'deadbeef1234',
            last_phase: 'fix',
            phase_log: ['create PR#11 iter1: built endpoint', 'fix PR#11 iter2: addressed review'],
            critical_files: ['backend/app/foo.py', 'agent/src/bar.ts'],
            remaining: ['[MAJOR] [a.ts] handle the null case'],
            risks: ['disputed: the flag is intentional'],
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Lane Resume — accumulated chain knowledge (checkpoint 3, last phase fix)');
    expect(prompt).toContain('Resumed from checkpoint SHA `deadbeef1234`.');
    expect(prompt).toContain('Prior phases of this chain:');
    expect(prompt).toContain('- create PR#11 iter1: built endpoint');
    expect(prompt).toContain('Files changed across the chain:');
    expect(prompt).toContain('- backend/app/foo.py');
    expect(prompt).toContain('Open issues carried into this fix:');
    expect(prompt).toContain('Known risks / deferred items:');
    // Additive — current review feedback remains present.
    expect(prompt).toContain('Current review feedback to address:\naddress review');
  });

  it('omits the ## Lane Resume section when context.lane_resume is absent', async () => {
    await handleFix(
      'task-no-lane-resume-fix',
      { branch: 'feat/y', base_branch: 'main', pr_number: 12, prompt: 'address review' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: hist } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).not.toContain('## Lane Resume');
  });
});

describe('formatLaneResumeSection — pure formatter', () => {
  it('returns empty string for undefined / non-object input', () => {
    expect(formatLaneResumeSection(undefined)).toBe('');
    expect(formatLaneResumeSection(null)).toBe('');
  });

  it('returns empty string when the block has no usable signal', () => {
    // No lists and no checkpoint sha — nothing to resume from.
    expect(formatLaneResumeSection({ checkpoint_seq: 0 })).toBe('');
    expect(formatLaneResumeSection({ phase_log: [] })).toBe('');
  });

  it('renders a sha-only block even with no list fields', () => {
    const out = formatLaneResumeSection({ last_checkpoint_sha: 'abc1234', checkpoint_seq: 1 });
    expect(out).toContain('## Lane Resume — accumulated chain knowledge (checkpoint 1)');
    expect(out).toContain('Resumed from checkpoint SHA `abc1234`.');
  });

  it('caps rendered list items per field at the render max', () => {
    const many = Array.from({ length: 30 }, (_, i) => `file-${i}.py`);
    const out = formatLaneResumeSection({ last_checkpoint_sha: 'abc1234', critical_files: many });
    expect(out).toContain('- file-0.py');
    expect(out).toContain('- file-11.py');
    // 13th item (index 12) must be dropped by the 12-item render cap.
    expect(out).not.toContain('- file-12.py');
  });

  it('ignores non-string list entries defensively', () => {
    const out = formatLaneResumeSection({
      last_checkpoint_sha: 'abc1234',
      critical_files: ['ok.py', 42, null, 'also-ok.ts'],
    });
    expect(out).toContain('- ok.py');
    expect(out).toContain('- also-ok.ts');
    expect(out).not.toContain('- 42');
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
    // 9 files > small threshold of 8 (SCOPE_OVERFLOW_THRESHOLDS.small).
    vi.mocked(indexMod.git).mockImplementation((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(9);
      }
      return '';
    });

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
    // 3 files within small threshold of 8 (SCOPE_OVERFLOW_THRESHOLDS.small).
    vi.mocked(indexMod.git).mockImplementation((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(3);
      }
      return '';
    });

    await handleReview(
      'task-no-overflow', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('apply full rigor to what the diff actually touches');
    expect(prompt).toContain('A small diff does not discount contract defects');
    expect(prompt).not.toContain('exceeds the typical range');
  });

  it('medium-scope PR exceeding threshold replaces empty preamble with overflow note', async () => {
    // 17 files > medium threshold of 16 (SCOPE_OVERFLOW_THRESHOLDS.medium).
    vi.mocked(indexMod.git).mockImplementation((args: string[]) => {
      if (Array.isArray(args) && args[0] === 'diff' && args[1] === '--name-status') {
        return fakeDiffNameStatus(17);
      }
      return '';
    });

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
    const hint = detectMediumScopeSubsystemHint('M\tagent/src/handlers/standard.ts\nM\tagent/src/prompt.ts', BLUEPRINT_CONFIG);
    expect(hint).toContain('agent/');
    expect(hint).toContain('deprioritize SQL injection');
  });

  it('returns node-type hint when all changed files are under admin-ui/', () => {
    const hint = detectMediumScopeSubsystemHint('M\tadmin-ui/src/App.tsx\nA\tadmin-ui/src/components/Foo.tsx', BLUEPRINT_CONFIG);
    expect(hint).toContain('admin-ui/');
    expect(hint).toContain('deprioritize SQL injection');
  });

  it('keeps full security scrutiny for backend-only changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/app/orchestrator.py\nM\tbackend/app/main.py', BLUEPRINT_CONFIG);
    expect(hint).toContain('backend/');
    expect(hint).toContain('full security scrutiny');
  });

  it('deprioritizes security for backend/testing-only changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/testing/test_foo.py\nA\tbackend/testing/test_bar.py', BLUEPRINT_CONFIG);
    expect(hint).toContain('test');
    expect(hint).toContain('Deprioritize');
    expect(hint).not.toContain('full security scrutiny');
  });

  it('keeps full security scrutiny when backend changes include a non-test file', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/testing/test_foo.py\nM\tbackend/app/main.py', BLUEPRINT_CONFIG);
    expect(hint).toContain('full security scrutiny');
  });

  it('returns cross-system guidance when changes span 2+ subsystems', () => {
    const hint = detectMediumScopeSubsystemHint('M\tagent/src/index.ts\nM\tbackend/app/main.py', BLUEPRINT_CONFIG);
    expect(hint).toContain('cross-system');
    expect(hint).toContain('agent');
    expect(hint).toContain('backend');
    expect(hint).toContain('interface consistency');
  });

  it('returns cross-system guidance for backend + admin-ui changes', () => {
    const hint = detectMediumScopeSubsystemHint('M\tbackend/app/main.py\nM\tadmin-ui/src/App.tsx', BLUEPRINT_CONFIG);
    expect(hint).toContain('cross-system');
    expect(hint).toContain('admin-ui');
    expect(hint).toContain('backend');
  });

  it('returns empty string for changed files placeholder / no files', () => {
    expect(detectMediumScopeSubsystemHint('', BLUEPRINT_CONFIG)).toBe('');
    expect(detectMediumScopeSubsystemHint('(could not generate file list)', BLUEPRINT_CONFIG)).toBe('');
  });

  it('returns empty string when no buildConfig is provided', () => {
    expect(detectMediumScopeSubsystemHint('M\tagent/src/index.ts')).toBe('');
    expect(detectMediumScopeSubsystemHint('M\tbackend/app/main.py')).toBe('');
  });

  it('returns node hint for a custom single-directory node project', () => {
    const config = { directories: [{ path: 'src', type: 'node' }] };
    const hint = detectMediumScopeSubsystemHint('M\tsrc/index.ts\nM\tsrc/utils.ts', config);
    expect(hint).toContain('src/');
    expect(hint).toContain('deprioritize SQL injection');
  });

  it('returns empty string for paths outside any configured directory', () => {
    const config = { directories: [{ path: 'src', type: 'node' }] };
    expect(detectMediumScopeSubsystemHint('M\tdocs/readme.md', config)).toBe('');
  });

  it('returns cross-system hint for two node directories', () => {
    const config = { directories: [{ path: 'app', type: 'node' }, { path: 'lib', type: 'node' }] };
    const hint = detectMediumScopeSubsystemHint('M\tapp/index.ts\nM\tlib/utils.ts', config);
    expect(hint).toContain('cross-system');
    expect(hint).toContain('app');
    expect(hint).toContain('lib');
    expect(hint).toContain('interface consistency');
  });

  it('handles other type directories with honest guidance (not mislabeled as Node)', () => {
    const config = { directories: [{ path: 'ruby-svc', type: 'other' }] };
    const hint = detectMediumScopeSubsystemHint('M\truby-svc/lib/handler.rb\nM\truby-svc/config.rb', config);
    expect(hint).toContain('ruby-svc/');
    expect(hint).not.toContain('TypeScript');
    expect(hint).not.toContain('Node.js');
    expect(hint).toContain('external family');
    expect(hint).toContain('ecosystem');
  });

  it('includes other type in merge verdict when mixed with core families', () => {
    const config = {
      directories: [
        { path: 'backend', type: 'python' },
        { path: 'ruby-svc', type: 'other' },
      ],
    };
    const hint = detectMediumScopeSubsystemHint('M\tbackend/app/main.py\nM\truby-svc/lib/handler.rb', config);
    expect(hint).toContain('cross-system');
    expect(hint).toContain('backend');
    expect(hint).toContain('ruby-svc');
    expect(hint).toContain('interface consistency');
  });
});

describe('handleReview — medium-scope subsystem hint integration', () => {
  it('injects agent-focused hint into the review prompt for medium-scope agent-only PRs', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce(BLUEPRINT_CONFIG);
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/handlers/review-fix.ts';
      }
      return '';
    });

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

  it('scopes build_config lint guidance to changed files in the review prompt', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python', lint_cmd: 'ruff check .' }],
    });
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tsvc/a.py';
      }
      return '';
    });

    await handleReview(
      'task-review-lint-scope',
      { pr_number: 51, branch: 'feature/lint', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('svc/a.py');
    expect(prompt).not.toContain('ruff check .');
    expect(prompt).not.toContain('grep \'.py$\'');
  });

  it.each([
    ['mypy', 'python3 -m mypy'],
    ['flake8', 'flake8'],
    ['pyright', 'pnpm exec pyright'],
  ])('scopes %s build_config lint guidance to changed Python files with the configured binary', async (_label, lintCmd) => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python', lint_cmd: lintCmd }],
    });
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tsvc/a.py';
      }
      return '';
    });

    await handleReview(
      'task-review-py-lint-scope',
      { pr_number: 52, branch: 'feature/lint-py', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('svc/a.py');
    expect(prompt).toContain(`xargs -r ${lintCmd}`);
    expect(prompt).not.toContain('python3 -m ruff check --select E,F,W --ignore E501');
    expect(prompt).not.toContain('ruff check .');
  });

  it.each([
    ['ruff check .'],
    ['python3 -m mypy'],
    ['flake8'],
    ['npx eslint'],
  ])('review prompt contains exact buildScopedLintOneliner output for %s (drift guard)', async (lintCmd) => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [{ path: 'svc', type: 'python', lint_cmd: lintCmd }],
    });
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) return 'M\tsvc/a.py';
      return '';
    });

    await handleReview(
      `task-lint-drift-${lintCmd.replace(/\W/g, '-')}`,
      { pr_number: 98, branch: 'feat/drift', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    const expected = buildScopedLintOneliner(lintCmd, 'svc', 'main', 'git-diff');
    if (expected === null) throw new Error(`buildScopedLintOneliner returned null for ${lintCmd}`);
    expect(prompt).toContain(expected);
  });

  it('injects cross-system hint for multi-subsystem medium-scope PRs', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce(BLUEPRINT_CONFIG);
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/index.ts\nM\tbackend/app/main.py';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/handlers/review-fix.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/prompt.ts\nA\tagent/src/handlers/review-fix.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\ta.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tbackend/app/main.py\nA\tbackend/app/new.py';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        // The handler defaults to "(could not generate file list)" on failure;
        // simulate that path by returning the same sentinel.
        return '(could not generate file list)';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        // planned: src/auth.ts; unplanned non-test: src/main.ts
        return 'M\tagent/src/auth.ts\nM\tagent/src/main.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/auth.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/main.ts';
      }
      return '';
    });

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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tagent/src/auth.ts\nA\tagent/src/auth.test.ts';
      }
      return '';
    });

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

  it('accepts a markdown-wrapped well-formed verdict from the /tmp file without a repair pass', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      '```json\n{"verdict":"APPROVE","feedback":"wrapped but well-formed"}\n```',
    );
    // The model wrote the verdict ONLY to the file (wrapped in a fence) and its
    // final output text carries no standalone verdict JSON. Before the contract
    // fix, strict JSON.parse rejected the fence and a repair reopen burned a
    // miss + attempt; the string-aware reader must accept it on the first pass.
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'review output without a standalone verdict JSON',
      lastResultText: 'review text without a standalone verdict JSON',
    });

    await handleReview(
      'task-wrapped', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalledTimes(1);
    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.status).toBe('COMPLETED');
    const verdictJson = JSON.parse(String(status.verdict_json)) as { verdict: string; feedback: string };
    expect(verdictJson.verdict).toBe('APPROVE');
    expect(verdictJson.feedback).toBe('wrapped but well-formed');
    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: {},
      format_repair_miss_by_reason: {},
      format_error_total: 0,
      format_repair_attempts: 0,
      format_repair_successes: 0,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: {},
      format_repair_attempts_by_schema: {},
      format_repair_successes_by_schema: {},
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: {},
    });
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

  it('bounds an oversized augmented verdict before posting agent status', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'REQUEST_CHANGES',
      feedback: 'f'.repeat(20_000),
      issues: [{
        issue_id: 'BP-oversized',
        severity: 'major',
        description: 'd'.repeat(20_000),
        file: 'agent/src/handlers/review-fix.ts',
        line: 960,
      }],
    }));
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });
    vi.mocked(indexMod.extractInvestigatedFiles).mockReturnValueOnce([
      'agent/src/handlers/review-fix.ts',
    ]);

    await handleReview(
      'task-oversized', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const serialized = String(status.verdict_json);
    const verdictJson = JSON.parse(serialized) as {
      verdict: string;
      issues: Array<Record<string, unknown>>;
      investigated_files: string[];
      verdict_json_truncated: boolean;
    };
    expect(serialized.length).toBeLessThanOrEqual(REVIEW_VERDICT_JSON_MAX_LENGTH);
    expect(verdictJson.verdict).toBe('REQUEST_CHANGES');
    expect(verdictJson.issues[0]).toMatchObject({
      issue_id: 'BP-oversized',
      severity: 'major',
      file: 'agent/src/handlers/review-fix.ts',
      line: 960,
    });
    expect(verdictJson.investigated_files).toEqual([
      'agent/src/handlers/review-fix.ts',
    ]);
    expect(verdictJson.verdict_json_truncated).toBe(true);
  });

  it('repairs a clean-exit review verdict when the temp file is malformed and preserves augmentation', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('not json at all');
    vi.mocked(indexMod.runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"verdict":"REQUEST_CHANGES","feedback":"incomplete",}',
        lastResultText: '{"verdict":"REQUEST_CHANGES","feedback":"incomplete",}',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"verdict":"REQUEST_CHANGES","feedback":"repair succeeded"}',
        lastResultText: '{"verdict":"REQUEST_CHANGES","feedback":"repair succeeded"}',
      });
    vi.mocked(indexMod.extractInvestigatedFiles).mockReturnValueOnce([
      'agent/src/handlers/review-fix.ts',
    ]);

    await handleReview(
      'task-2', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalledTimes(2);
    const repairPrompt = vi.mocked(indexMod.runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain('ONLY the JSON object');
    expect(repairPrompt).toContain('/tmp/review_verdict_task-2.json');

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.status).toBe('COMPLETED');
    const verdictJson = JSON.parse(String(status.verdict_json)) as { verdict: string; feedback: string; investigated_files: string[] };
    expect(verdictJson.verdict).toBe('REQUEST_CHANGES');
    expect(verdictJson.feedback).toBe('repair succeeded');
    expect(verdictJson.investigated_files).toEqual(['agent/src/handlers/review-fix.ts']);
    expect(String(status.logs)).toContain('"verdict":"REQUEST_CHANGES"');

    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: { malformed_json: 1 },
      format_repair_miss_by_reason: {},
      format_error_total: 1,
      format_repair_attempts: 1,
      format_repair_successes: 1,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: { review_verdict: 1 },
      format_repair_attempts_by_schema: { review_verdict: 1 },
      format_repair_successes_by_schema: { review_verdict: 1 },
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: { review_verdict: { malformed_json: 1 } },
    });
  });

  it('repairs a clean-exit review verdict when the temp file is absent and preserves augmentation', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    vi.mocked(indexMod.runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'plain review output with no JSON',
        lastResultText: 'plain review output with no JSON',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"verdict":"APPROVE","feedback":"repaired verdict"}',
        lastResultText: '{"verdict":"APPROVE","feedback":"repaired verdict"}',
      });
    vi.mocked(indexMod.extractInvestigatedFiles).mockReturnValueOnce([
      'agent/src/handlers/review-fix.ts',
      'agent/src/prompts/review-prompt.ts',
    ]);

    await handleReview(
      'task-2b', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalledTimes(2);
    const repairPrompt = vi.mocked(indexMod.runClaudeCode).mock.calls[1][0];
    expect(repairPrompt).toContain('ONLY the JSON object');
    expect(repairPrompt).toContain('/tmp/review_verdict_task-2b.json');
    expect(repairPrompt).not.toContain('After your review');
    expect(repairPrompt).toContain('Your review analysis is complete');

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as { verdict: string; feedback: string; investigated_files: string[] };
    expect(verdictJson.verdict).toBe('APPROVE');
    expect(verdictJson.feedback).toBe('repaired verdict');
    expect(verdictJson.investigated_files).toEqual([
      'agent/src/handlers/review-fix.ts',
      'agent/src/prompts/review-prompt.ts',
    ]);
    expect(String(status.logs)).toContain('"verdict":"APPROVE"');

    expect(getFormatComplianceTelemetry()).toEqual({
      format_miss_by_reason: { missing_required_key: 1 },
      format_repair_miss_by_reason: {},
      format_error_total: 1,
      format_repair_attempts: 1,
      format_repair_successes: 1,
      format_repair_infra_failures: 0,
      format_repair_input_tokens: 0,
      format_repair_output_tokens: 0,
      format_miss_by_schema: { review_verdict: 1 },
      format_repair_attempts_by_schema: { review_verdict: 1 },
      format_repair_successes_by_schema: { review_verdict: 1 },
      format_repair_infra_failures_by_schema: {},
      format_repair_input_tokens_by_schema: {},
      format_repair_output_tokens_by_schema: {},
      format_repair_miss_by_schema: {},
      format_miss_by_schema_and_reason: { review_verdict: { missing_required_key: 1 } },
    });
  });

  it('repairs a clean-exit review verdict when the temp file has JSON without verdict field', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"feedback":"orphan"}');
    vi.mocked(indexMod.runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0, allOutput: 'no verdict in the first pass', lastResultText: 'no verdict in the first pass',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"verdict":"APPROVE","feedback":"repaired verdict"}',
        lastResultText: '{"verdict":"APPROVE","feedback":"repaired verdict"}',
      });

    await handleReview(
      'task-3', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalledTimes(2);
    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as { verdict: string; feedback: string };
    expect(verdictJson.verdict).toBe('APPROVE');
    expect(verdictJson.feedback).toBe('repaired verdict');
    expect(status.verdict_json).toBeDefined();
    expect(String(status.logs)).toContain('"verdict":"APPROVE"');
  });

  it('includes reviewed head and fetched base SHAs in the augmented verdict JSON', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      '{"verdict":"REQUEST_CHANGES","feedback":"a bug"}',
    );
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: '',
    });
    // Simulate git rev-parse HEAD returning a SHA after clone.
    vi.mocked(indexMod.git).mockImplementation((args: string[]) => {
      if (args[0] === 'rev-parse' && args[1] === 'HEAD') return 'abc123def456789';
      if (args[0] === 'rev-parse' && args[1] === 'origin/main') return 'base123def456789';
      return '';
    });

    await handleReview(
      'task-head-sha', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as {
      verdict: string; reviewed_head_sha?: string; reviewed_base_sha?: string;
    };
    expect(verdictJson.reviewed_head_sha).toBe('abc123def456789');
    expect(verdictJson.reviewed_base_sha).toBe('base123def456789');
    expect(verdictJson.verdict).toBe('REQUEST_CHANGES');
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
      { foo: 'bar' }, 'review', 'small', 'd-abc123', 0,
    );
  });

  it('forwards the related-files count to formatProjectContext', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatProjectContext).mockClear();
    await handleReview(
      'task-pc-rf',
      { ...basePayload, related_files: ['a.ts', 'b.ts'] },
      { context: { scope: 'medium', direction_id: 'd-xyz' }, project_context: { foo: 'bar' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(promptMod.formatProjectContext).toHaveBeenCalledWith(
      { foo: 'bar' }, 'review', 'medium', 'd-xyz', 2,
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

    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalledTimes(1);
    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    expect(status.status).toBe('FAILED');
    expect(status.failure_category).toBe('claude_code_failed');
    expect(status.artifact_type).toBe('ERROR_LOG');
  });

  it('sets the review prompt fingerprint and emits an initial heartbeat before running Claude', async () => {
    await handleReview(
      'task-fingerprint', basePayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(indexMod.setCurrentPromptFingerprint).toHaveBeenCalledTimes(1);
    const fingerprint = vi.mocked(indexMod.setCurrentPromptFingerprint).mock.calls[0][0];
    expect(fingerprint).toMatch(/^[0-9a-f]{12}$/);
    expect(indexMod.sendHeartbeatSync).toHaveBeenCalledWith(
      'task-fingerprint',
      'Running code review...',
      { prompt_fingerprint: fingerprint },
    );
  });
});

// ── handleReview: project vision and strategic intent (direction goal) ────────
describe('handleReview — project vision and strategic intent (direction goal)', () => {
  const basePayload = {
    pr_number: 50,
    branch: 'feature/strategic',
    base_branch: 'main',
    original_prompt: 'Review the changes',
  };

  it('surfaces project vision in the review prompt when vision is present', async () => {
    await handleReview(
      'task-review-vision',
      basePayload,
      {
        context: { scope: 'medium' },
        vision: {
          purpose: 'Ship the right user outcome.',
          success_criteria: ['The fix is validated by the review feedback.'],
          priorities: ['Keep the change minimal.'],
          context: 'Reviewers should understand why this work matters.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Project Vision');
    expect(prompt).toContain('**Purpose:** Ship the right user outcome.');
    expect(prompt).toContain('**Success criteria:**');
    expect(prompt).toContain('- The fix is validated by the review feedback.');
    expect(prompt).toContain('**Priorities:**');
    expect(prompt).toContain('- Keep the change minimal.');
    expect(prompt).toContain('**Context:** Reviewers should understand why this work matters.');
  });

  it('surfaces strategic intent in the review prompt when strategic_intent is present', async () => {
    await handleReview(
      'task-review-steering',
      basePayload,
      {
        context: { scope: 'medium' },
        strategic_intent: {
          purpose: 'Build an autonomous coding studio.',
          current_focus: 'Thread operator steering into tactical prompts.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Operator Steering');
    expect(prompt).toContain('**Purpose:** Build an autonomous coding studio.');
    expect(prompt).toContain('**Current focus:** Thread operator steering into tactical prompts.');
  });

  it('surfaces both vision and strategic intent in the review prompt when both are present', async () => {
    await handleReview(
      'task-review-both',
      basePayload,
      {
        context: { scope: 'medium' },
        vision: {
          purpose: 'Ship the right user outcome.',
          success_criteria: ['The fix is validated.'],
          priorities: ['Keep it minimal.'],
          context: 'Why this matters.',
        },
        strategic_intent: {
          purpose: 'Build an autonomous studio.',
          current_focus: 'Strategic steering.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    // Both sections must be present
    expect(prompt).toContain('## Project Vision');
    expect(prompt).toContain('Ship the right user outcome.');
    expect(prompt).toContain('## Operator Steering');
    expect(prompt).toContain('Build an autonomous studio.');
    // Vision must come before Operator Steering
    const visionIdx = prompt.indexOf('## Project Vision');
    const steeringIdx = prompt.indexOf('## Operator Steering');
    expect(visionIdx).toBeLessThan(steeringIdx);
  });

  it('omits strategic framing sections when neither vision nor strategic_intent is present', async () => {
    await handleReview(
      'task-review-no-framing',
      basePayload,
      { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Project Vision');
    expect(prompt).not.toContain('## Operator Steering');
    // But the review still works with base content
    expect(prompt).toContain('Review the changes');
  });

  it('surfaces repo overview in the review prompt when repo_overview is present', async () => {
    await handleReview(
      'task-review-repo-overview',
      basePayload,
      {
        context: { scope: 'medium' },
        repo_overview: {
          tech_stack: 'Python, FastAPI, React',
          directory_layout: 'backend/ agent/ admin-ui/',
          first_steps_summary: 'pip install && npm install',
          bootstrap_completed_at: '2026-01-15T10:00:00Z',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Repo Overview');
    expect(prompt).toContain('**Tech stack:** Python, FastAPI, React');
    expect(prompt).toContain('**Directory layout:** backend/ agent/ admin-ui/');
    expect(prompt).toContain('**First steps:** pip install && npm install');
    expect(prompt).toContain('**Bootstrapped:** 2026-01-15T10:00:00Z');
  });

  it('omits repo overview from the review prompt when repo_overview is absent', async () => {
    await handleReview(
      'task-review-no-repo-overview',
      basePayload,
      { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Repo Overview');
  });
});

// ── handleReview: deleted-head-branch protection ─────────────────────────────
describe('handleReview — deleted head branch short-circuit', () => {
  const basePayload = {
    pr_number: 999,
    branch: 'feature/deleted',
    base_branch: 'main',
    original_prompt: '',
  };

  it('completes cleanly with PR_BRANCH_DELETED when branch no longer exists on remote', async () => {
    // ls-remote returns empty string = branch does not exist
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && Array.isArray(args) && args.includes('ls-remote')) {
        return '';
      }
      return '';
    });

    const { prepareWorkspaceForRole } = await import('../workspacePrep.js');

    await handleReview(
      'task-deleted-branch', basePayload, { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // Must not have cloned or run Claude
    expect(vi.mocked(prepareWorkspaceForRole)).not.toHaveBeenCalled();
    expect(vi.mocked(indexMod.runClaudeCode)).not.toHaveBeenCalled();

    const statusCalls = vi.mocked(indexMod.persistAndPostStatus).mock.calls;
    expect(statusCalls.length).toBeGreaterThanOrEqual(1);
    const status = statusCalls[0][1];
    expect(status.status).toBe('COMPLETED');
    expect(status.failure_category).toBe('PR_BRANCH_DELETED');
    expect(String(status.logs)).toContain('feature/deleted');
  });

  it('proceeds to clone and review when branch exists on remote', async () => {
    // ls-remote returns a ref line = branch exists
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && Array.isArray(args) && args.includes('ls-remote')) {
        return 'abc123\trefs/heads/feature/deleted\n';
      }
      return '';
    });

    const { prepareWorkspaceForRole } = await import('../workspacePrep.js');

    await handleReview(
      'task-existing-branch',
      { pr_number: 100, branch: 'feature/existing', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(prepareWorkspaceForRole)).toHaveBeenCalled();
    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalled();
  });

  it('falls through to clone when ls-remote throws (transport/auth failure)', async () => {
    // ls-remote throws = network/auth error; clone should still be attempted
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && Array.isArray(args) && args.includes('ls-remote')) {
        throw new Error('fatal: unable to look up github.com');
      }
      return '';
    });

    const { prepareWorkspaceForRole } = await import('../workspacePrep.js');

    await handleReview(
      'task-ls-remote-failure',
      { pr_number: 200, branch: 'feature/transport-fail', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // Clone is still attempted despite ls-remote failure
    expect(vi.mocked(prepareWorkspaceForRole)).toHaveBeenCalled();
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

  it('sets testsOnMainBranch=true via integration_branch cascade when promotion_target absent and baseBranch=master', async () => {
    await handleFix(
      'task-fix-integration-branch-master',
      { ...basePayload, base_branch: 'master' },
      { context: { scope: 'small' }, project_metadata: { integration_branch: 'master' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('master');
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('sets testsOnMainBranch=false when baseBranch does not match integration_branch', async () => {
    await handleFix(
      'task-fix-integration-branch-mismatch',
      { ...basePayload, base_branch: 'feature/x' },
      { context: { scope: 'small' }, project_metadata: { integration_branch: 'master' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('feature/x');
    expect(opts.testsOnMainBranch).toBe(false);
  });

  it('sets testsOnMainBranch=true when base_branch matches promotion_target', async () => {
    await handleFix(
      'task-fix-promotion-target',
      { ...basePayload, base_branch: 'automain', promotion_target: 'automain' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('automain');
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('sets testsOnMainBranch=false when base_branch does not match promotion_target', async () => {
    await handleFix(
      'task-fix-non-lts',
      { ...basePayload, base_branch: 'feature/x', promotion_target: 'automain' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('feature/x');
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

  it('rebases onto base branch before delegating to runCodeChangeTask', async () => {
    const seenGitArgs: string[][] = [];
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args) seenGitArgs.push(args);
      return '';
    });

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

  it('falls back to project_metadata.integration_branch when payload.base_branch is absent', async () => {
    await handleFix(
      'task-fix-meta-branch',
      { branch: 'feature/x', pr_number: 42, prompt: 'fix it' },
      { context: { scope: 'small' }, project_metadata: { integration_branch: 'develop' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('develop');
  });

  it('falls back to main when both payload.base_branch and project_metadata.integration_branch are absent', async () => {
    await handleFix(
      'task-fix-fallback-main',
      { branch: 'feature/x', pr_number: 42, prompt: 'fix it' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    expect(opts.baseBranch).toBe('main');
  });

  it('picks up promotionTarget from project_metadata when payload omits it', async () => {
    await handleFix(
      'task-fix-meta-promotion',
      { branch: 'feature/x', base_branch: 'trunk', pr_number: 42, prompt: 'fix it', promotion_target: 'trunk' },
      { context: { scope: 'small' }, project_metadata: { promotion_target: 'trunk' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    // base_branch matches promotion_target → testsOnMainBranch=true
    expect(opts.testsOnMainBranch).toBe(true);
  });

  it('falls back to project_metadata.promotion_target when payload.promotion_target is absent', async () => {
    await handleFix(
      'task-fix-meta-promo-fallback',
      { branch: 'feature/x', base_branch: 'trunk', pr_number: 42, prompt: 'fix it' },
      { context: { scope: 'small' }, project_metadata: { promotion_target: 'trunk' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const opts = capturedFixOpts();
    // base_branch 'trunk' matches project_metadata.promotion_target 'trunk' → testsOnMainBranch=true
    expect(opts.testsOnMainBranch).toBe(true);
  });
});

// ── handleFix: self-review blind-spots surfacing ────────────────────────────
describe('handleFix — self-review blind-spots surfacing', () => {
  it('passes task.context._self_review_outcome and the changed-file path list to formatSelfReviewBlindSpots, and prepends the returned section to the prompt', async () => {
    const promptMod = await import('../prompt.js');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('FIX_BLIND_SPOTS_MARKER\n\n');
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-only')) {
        return 'agent/src/prompts/self-review.ts\nagent/src/handlers/review-fix.ts';
      }
      return '';
    });

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

describe('handleFix — acceptance evidence is in prompt', () => {
  it('prepends the acceptance-evidence block when declared', async () => {
    await handleFix(
      'task-fix-acceptance',
      {
        branch: 'feature/x',
        base_branch: 'main',
        pr_number: 7,
        prompt: 'address review feedback',
        acceptance_evidence: {
          kind: 'named_test',
          test_path: 'testing/test_widget.py',
          test_id: 'test_widget_handles_empty',
        },
      },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Acceptance Evidence');
    expect(prompt).toContain('testing/test_widget.py');
    expect(prompt).toContain('test_widget_handles_empty');
    expect(prompt).toContain('real merge-test verdict');
  });
});

describe('handleFix — direction implementation constraints', () => {
  it('surfaces implementation_constraints from direction_context in the enriched prompt', async () => {
    await handleFix(
      'task-fix-impl-constraints',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      {
        context: { scope: 'small' },
        direction_context: { implementation_constraints: 'Must not modify the public API surface.' },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Implementation Constraints');
    expect(prompt).toContain('Must not modify the public API surface.');
  });

  it('omits the Implementation Constraints section when direction_context has none', async () => {
    await handleFix(
      'task-fix-no-impl-constraints',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'address review feedback' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('## Implementation Constraints');
  });
});

// ── handleFix: conflict marker + stale-premise detection ────────────────────
describe('handleFix — conflict marker detection', () => {
  it('enriches the prompt with a CONFLICT MARKERS FOUND header when git grep finds markers', async () => {
    // No unmerged paths (git mock returns ''), but grep finds marker-bearing files.
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) {
        return 'backend/app/foo.py\nagent/src/bar.ts\n';
      }
      return '';
    });

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

    expect(vi.mocked(testLoopMod.captureTestBaseline)).toHaveBeenCalledTimes(1);
  });

  it('skips baseline for CI-failure fixes so existing PR failures remain in scope', async () => {
    await handleFix(
      'task-fix-baseline-2',
      { branch: 'feature/x', base_branch: 'main', pr_number: 7, prompt: 'fix CI failures' },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(vi.mocked(testLoopMod.captureTestBaseline)).not.toHaveBeenCalled();
    expectCalledWith(indexMod.sendLog,
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

    expectCalledWith(testLoopMod.testFixLoop,
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
    vi.mocked(testLoopMod.testFixLoop).mockResolvedValueOnce({
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

// ── ci_failure closed-loop verify gate ──────────────────────────────────────
// The CI fix loop was previously OPEN: Claude fixed, ran ./check (lint/typecheck
// + the standalone-3 backend smoke files), then ./pr open pushed — but ./check
// never ran the actual CI-failing selectors, so the fix shipped blind and CI
// re-failed (~20-min provision+CI round-trip burned per iteration). handleFix
// now threads the exact CI-failing selectors to runCodeChangeTask, which wires
// them into ./check so ./pr open can't push until they pass locally.
describe('handleFix — ci_failure selectors wired into the ./check gate', () => {
  it('forwards extracted selectors to runCodeChangeTask for a ci_failure fix', async () => {
    await handleFix(
      'task-fix-ci-gate-1',
      {
        branch: 'feature/x',
        base_branch: 'main',
        pr_number: 7,
        prompt: 'CI failed:\nFAILED backend/testing/test_models.py::test_create - AssertionError',
      },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // The selectors CI reported failing are forwarded so ./check can verify
    // them before ./pr open pushes (closing the open loop).
    expect(capturedFixOpts().ciFailureSelectors).toEqual([
      'backend/testing/test_models.py::test_create',
    ]);
  });

  it('tells the agent the selectors are now part of the ./check gate', async () => {
    vi.mocked(testLoopMod.testFixLoop).mockResolvedValueOnce({
      passed: false,
      testFailuresDetected: true,
      failureSummary: 'AssertionError at backend/testing/test_models.py::test_create',
    });

    await handleFix(
      'task-fix-ci-gate-2',
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
    // Closed-loop framing: ./check runs the selectors, ./pr open blocks on them.
    expect(prompt).toMatch(/\.\/check runs them[\s\S]*\.\/pr open will NOT push until they pass/);
  });

  it('does NOT wire selectors for a non-ci_failure (review-feedback) fix', async () => {
    await handleFix(
      'task-fix-ci-gate-3',
      {
        branch: 'feature/x',
        base_branch: 'main',
        // A review-feedback fix prompt that incidentally mentions a failing
        // test must NOT trip the gate — only fixReason === 'ci_failure' does.
        pr_number: 7,
        prompt: 'Address review feedback. FAILED backend/testing/test_models.py::test_create',
      },
      { context: { scope: 'medium', review_loop: { fix_reason: 'code_review' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // Non-ci_failure fixes keep the existing gate untouched: no selectors, no
    // gate note in the prompt.
    const selectors = capturedFixOpts().ciFailureSelectors;
    expect(selectors === undefined || selectors.length === 0).toBe(true);
    expect(capturedFixPrompt()).not.toContain('.ci/pr open will NOT push');
    expect(capturedFixPrompt()).not.toMatch(/\.\/pr open will NOT push until they pass/);
  });

  it('forwards an empty selector list when none can be extracted from a ci_failure', async () => {
    await handleFix(
      'task-fix-ci-gate-4',
      {
        branch: 'feature/x',
        base_branch: 'main',
        pr_number: 7,
        // No parseable pytest selector in the logs.
        prompt: 'CI failed: the deploy step timed out, no test node-ids present',
      },
      { context: { scope: 'medium', review_loop: { fix_reason: 'ci_failure' }, related_files: ['backend/app/foo.py'] } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // ci_failure with no extractable selectors degrades to an empty list (no
    // gate run) rather than blocking — CI remains the backstop.
    expect(capturedFixOpts().ciFailureSelectors).toEqual([]);
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
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
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
    });

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
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
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
    });

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
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
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
    });

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
    vi.mocked(execFileSync).mockImplementation((cmd: string, args?: readonly string[]) => {
      if (cmd === 'git' && args && args.includes('grep')) return '';
      // curl should NOT be called in this branch; return empty defensively.
      return '';
    });

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

  it('permits discovered related tests when convergence scope starts from a reviewed source file', async () => {
    vi.mocked(promptMod.discoverRelatedTests).mockReturnValue(
      '## Related Tests\nThese tests cover files you\'ll be modifying:\n' +
      '- DEN/testing/test_results_analysis.py: test_clip_stats_admission\n',
    );
    await handleFix(
      'task-fix-related-regression',
      { branch: 'feature/x', base_branch: 'main', pr_number: 50, prompt: 'add the requested regression' },
      {
        context: {
          scope: 'medium',
          review_loop: {
            iteration: 6,
            review_history: [{
              iteration: 5,
              feedback: 'The source fix needs direct regression coverage.',
              issues: [{
                severity: 'major',
                description: 'Add a focused regression for this admission path.',
                file: 'DEN/results_analysis.py',
                line: 3349,
              }],
            }],
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    expect(promptMod.discoverRelatedTests).toHaveBeenCalledWith(
      null,
      ['DEN/results_analysis.py'],
      expect.anything(),
    );
    expect(capturedFixPrompt()).toContain(
      'Files you may modify: DEN/results_analysis.py, DEN/testing/test_results_analysis.py',
    );
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
    expectCalledWith(indexMod.gitAsync, ['push', '--force-with-lease', 'origin', 'feature/x']);
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
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (!args) return '';
      if (args[0] === 'rebase' && args[1] !== '--abort') {
        throw new Error('CONFLICT: cannot rebase');
      }
      if (args[0] === 'rebase' && args[1] === '--abort') {
        abortCalls.push(args);
        return '';
      }
      return '';
    });

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

  it.each([
    [
      'workflow permission',
      'remote: refusing to allow a GitHub App to create or update workflow .github/workflows/ci.yml without workflows permission',
      /cannot create or update workflow files without the workflows permission/,
    ],
    [
      'push protection',
      'remote: GH013: Repository rule violations found. secret scanning blocked this push',
      /push protection or secret scanning blocked it/,
    ],
    [
      'protected branch',
      'remote: GH006: Protected branch update failed for refs/heads/feature/x',
      /branch is protected/,
    ],
  ])('classifies %s push rejections without retrying', async (_label, message, expected) => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error(message));

    await expect(pushForceWithLeaseRetrying('feature/x', null))
      .rejects.toThrow(expected);

    expect(vi.mocked(indexMod.gitAsync)).toHaveBeenCalledTimes(1);
    const gitCalls = vi.mocked(indexMod.git).mock.calls.map(c => c[0]);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'fetch')).toBe(false);
    expect(gitCalls.some(args => Array.isArray(args) && args[0] === 'rebase')).toBe(false);
  });

  it('silently ignores a rebase --abort failure and still throws the descriptive conflict error', async () => {
    vi.mocked(indexMod.gitAsync).mockRejectedValueOnce(new Error('stale info'));
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (!args) return '';
      if (args[0] === 'rebase' && args[1] !== '--abort') {
        throw new Error('CONFLICT during rebase');
      }
      if (args[0] === 'rebase' && args[1] === '--abort') {
        throw new Error('fatal: no rebase in progress');
      }
      return '';
    });

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

  it('build_config fix task gets project-aware ./check guidance (no Blueprint stacks)', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValueOnce({
      directories: [
        { path: 'web', type: 'node', lint_cmd: 'eslint src', build_cmd: 'npm run build', test_cmd: 'npm test' },
      ],
    });
    await handleFix('task-fix-bc', FIX_PAYLOAD, FIX_TASK, 'token', 'owner', 'repo', null, statusRef());
    const p = capturedFixPrompt();
    expect(p).toContain('Task lifecycle — you own it end-to-end');
    expect(p).toContain('./pr open');
    expect(p).toContain('./done');
    expect(p).not.toContain('scripts/test-backend.sh');
    expect(p).not.toContain('backend ruff');
    expect(p).not.toContain('agent tsc');
    expect(p).toContain('npm run build');
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

  it('does not include other type-specific blocks for unknown task types, but always includes fix-task focus', async () => {
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
      expect(p).not.toContain('Self-review focus: docs tasks');
      expect(p).not.toContain('Self-review focus: deps tasks');
      // FIX_TASK_FOCUS is always injected for fix iterations regardless of original type
      expect(p).toContain('Self-review focus: fix tasks');
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
    return calls[0][0];
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

  it('handleReview prompt instructs exhaustive enumeration of blocking issues', async () => {
    // One-blocker-per-iteration drip burned PR #6061 through 7 review cycles
    // before the iteration cap auto-closed it. The verdict instructions now
    // tell the reviewer to enumerate every blocker in one pass so the fix
    // agent can converge instead of chasing a moving target.
    const prompt = await captureReviewPrompt();
    expect(prompt).toContain('Exhaustiveness');
    expect(prompt).toContain('enumerate every blocker');
  });

  it('handleReview prompt contains Severity Classification Guide', async () => {
    expect(await captureReviewPrompt()).toContain('**Severity Classification Guide:**');
  });

  it('handleReview prompt contains verdict JSON file path pattern', async () => {
    expect(await captureReviewPrompt()).toContain('/tmp/review_verdict_');
  });

  it('handleReview prompt contains READ_ONLY_BASH_WRITE_NOTE next to /tmp verdict path', async () => {
    expect(await captureReviewPrompt()).toContain(READ_ONLY_BASH_WRITE_NOTE);
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

  it('handleReview fingerprints static review guidance rather than task-specific context', async () => {
    await handleReview('task-rev', REVIEW_PAYLOAD, REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef());
    const expected = computeReviewPromptFingerprint({});
    expectCalledWith(indexMod.setCurrentPromptFingerprint, expected);
    expectCalledWith(indexMod.sendHeartbeatSync,
      'task-rev',
      'Running code review...',
      { prompt_fingerprint: expected },
    );

    vi.clearAllMocks();
    await handleReview(
      'task-rev-2',
      { ...REVIEW_PAYLOAD, pr_number: 999, branch: 'feature/other', original_prompt: 'different task text' },
      {
        ...REVIEW_TASK,
        context: {
          ...(REVIEW_TASK.context as Record<string, unknown>),
          review_loop: { iteration: 5, review_history: [{ iteration: 4, feedback: 'prior feedback' }] },
        },
        project_context: { direction: 'other-direction' },
      },
      'token',
      'owner',
      'repo',
      null,
      statusRef(),
    );
    expectLastCalledWith(indexMod.setCurrentPromptFingerprint, expected);
  });
});

// ── handleReview: rollup-PR guidance honors per-project staging prefixes ─────
describe('handleReview — rollup-PR guidance staging prefixes', () => {
  const ROLLUP_MARKER = 'Rollup-PR verification';
  const REVIEW_TASK = { title: 'Review', description: '', context: {}, project_context: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  function lastReviewPrompt(): string {
    const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    return calls[0][0];
  }

  it('injects rollup guidance when a custom staging prefix matches the head branch', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({ staging_branch_prefixes: ['stage/', 'integration/'] });
    await handleReview(
      'task-rollup-custom',
      { pr_number: 10, branch: 'stage/abcd1234', base_branch: 'main' },
      REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).toContain(ROLLUP_MARKER);
  });

  it('does NOT inject rollup guidance when the head branch matches no configured prefix', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({ staging_branch_prefixes: ['stage/', 'integration/'] });
    await handleReview(
      'task-rollup-nomatch',
      { pr_number: 11, branch: 'feature/foo', base_branch: 'main' },
      REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(ROLLUP_MARKER);
  });

  it('default Blueprint config still injects rollup guidance for research/* heads (regression guard)', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    await handleReview(
      'task-rollup-default',
      { pr_number: 12, branch: 'research/4da143e3', base_branch: 'main' },
      REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).toContain(ROLLUP_MARKER);
  });

  it('default Blueprint config does NOT inject rollup guidance for a non-staging branch', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    await handleReview(
      'task-rollup-default-nonstaging',
      { pr_number: 13, branch: 'feat', base_branch: 'main' },
      REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(ROLLUP_MARKER);
  });

  // ── Iteration gating: re-verifying rollup non-emptiness every round is pure
  // overhead that pulls the reviewer into verification-only drift. By round 2
  // the PR is already established as a real, non-empty rollup, so the guidance
  // is injected only on iteration 1.
  it('injects rollup guidance on iteration 1 for a rollup head', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    await handleReview(
      'task-rollup-iter1',
      { pr_number: 14, branch: 'research/iter1', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 1 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).toContain(ROLLUP_MARKER);
  });

  it('does NOT inject rollup guidance on iteration 2 for a rollup head (already established non-empty)', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    await handleReview(
      'task-rollup-iter2',
      { pr_number: 15, branch: 'research/iter2', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 2 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(ROLLUP_MARKER);
  });

  it('does NOT inject rollup guidance on a late iteration for a rollup head', async () => {
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    await handleReview(
      'task-rollup-iter5',
      { pr_number: 16, branch: 'research/iter5', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 5 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(ROLLUP_MARKER);
  });

  it('fingerprint stays invariant to per-project staging prefixes (stable default)', () => {
    // The fingerprint must track template edits, not per-task config: a project
    // configuring custom prefixes must not fragment reviewer telemetry, so the
    // fingerprint always evaluates rollup detection against ['research/','plan/'].
    const baseFp = computeReviewPromptFingerprint(BLUEPRINT_CONFIG, { branch: 'research/abc', baseBranch: 'main' });
    const customFp = computeReviewPromptFingerprint(
      { ...BLUEPRINT_CONFIG, staging_branch_prefixes: ['stage/', 'integration/'] },
      { branch: 'research/abc', baseBranch: 'main' },
    );
    expect(customFp).toBe(baseFp);
  });
});

// ── handleReview: delta-focus guidance for later iterations ──────────────────
// Round 1 keeps the full-PR-diff full-rigor review (no delta-focus). From
// iteration ≥2 the reviewer is told to focus on the changes since the last fix
// commit plus the unresolved set, so it stops re-scanning the whole PR and
// re-deriving already-handed-off issues ("verification-only" drift).
describe('handleReview — delta-focus guidance (iteration ≥2)', () => {
  const DELTA_MARKER = 'Delta-focus (iteration ≥2)';
  const REVIEW_TASK = { title: 'Review', description: '', context: {}, project_context: {} };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(resolveBuildConfig).mockReturnValue({});
    vi.mocked(indexMod.runClaudeCode).mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
  });

  function lastReviewPrompt(): string {
    const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    return calls[0][0];
  }

  it('does NOT emit delta-focus guidance on iteration 1 (full-rigor first pass preserved)', async () => {
    await handleReview(
      'task-delta-iter1',
      { pr_number: 20, branch: 'feature/x', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 1 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(DELTA_MARKER);
  });

  it('does NOT emit delta-focus guidance when iteration is unset (defaults to 1)', async () => {
    await handleReview(
      'task-delta-default',
      { pr_number: 21, branch: 'feature/x', base_branch: 'main' },
      REVIEW_TASK, 'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain(DELTA_MARKER);
  });

  it('emits delta-focus guidance on iteration 2', async () => {
    await handleReview(
      'task-delta-iter2',
      { pr_number: 22, branch: 'feature/x', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 2 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = lastReviewPrompt();
    expect(prompt).toContain(DELTA_MARKER);
    // Rigor-preserving framing: a genuinely-unresolved prior blocker or a NEW
    // blocker the delta introduced still blocks merge.
    expect(prompt).toContain('only raise a NEW blocker if the delta introduced one');
    expect(prompt).toContain('Do NOT re-litigate already-addressed');
  });

  it('emits delta-focus guidance on a late iteration', async () => {
    await handleReview(
      'task-delta-iter5',
      { pr_number: 23, branch: 'feature/x', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 5 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).toContain(DELTA_MARKER);
  });
});

// ── handleReview: exact-SHA follow-up delta ─────────────────────────────────
describe('handleReview — exact-SHA delta execution', () => {
  const REVIEW_TASK = { title: 'Review', description: '', project_context: {} };

  function lastReviewPrompt(): string {
    const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    // Empty mocked output triggers the structured-verdict repair pass. The
    // first call is the review prompt whose delta scope we are asserting.
    return calls[0][0];
  }

  it('uses the supplied prior head as an exact two-dot diff and carries prior review context', async () => {
    const priorHead = 'a'.repeat(40);
    vi.mocked(promptMod.formatPlaybook).mockReturnValue('PLAYBOOK_CONTEXT');
    vi.mocked(promptMod.formatProjectContext).mockReturnValue('PROJECT_CONTEXT');
    vi.mocked(promptMod.formatSelfReviewBlindSpots).mockReturnValue('SELF_REVIEW_BLIND_SPOTS');
    const reviewHistory = [{
      iteration: 1,
      feedback: 'Validate the null guard.',
      issues: [{ severity: 'major', description: 'Missing null guard', file: 'agent/a.ts', line: 12 }],
    }];
    const fixerResponses = [{
      issue_summary: 'Missing null guard', source_iteration: 1, response_type: 'alternative',
      claim: 'Added an early return.', verify: ['npx vitest run agent/a.test.ts'],
    }];

    await handleReview(
      'task-exact-delta',
      { pr_number: 24, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      {
        ...REVIEW_TASK,
        project_context: { project: 'context' },
        playbook: { rule: 'avoid regressions' },
        context: {
          scope: 'medium', _planned_files: ['agent/a.ts'],
          review_loop: { iteration: 2, review_history: reviewHistory, fix_agent_responses: fixerResponses },
        },
      },
      'token', 'owner', 'repo', null, statusRef(),
    );

    expectCalledWith(indexMod.git, ['cat-file', '-e', `${priorHead}^{commit}`]);
    expectCalledWith(indexMod.git, ['diff', '--name-status', `${priorHead}..HEAD`]);
    expectCalledWith(indexMod.git, ['diff', '--shortstat', `${priorHead}..HEAD`]);
    expect(vi.mocked(indexMod.git).mock.calls.some(([args]) =>
      args[0] === 'fetch' && args.includes('+refs/heads/main:refs/remotes/origin/main'),
    )).toBe(false);

    const prompt = lastReviewPrompt();
    expect(prompt).toContain(`git diff ${priorHead}..HEAD`);
    expect(prompt).toContain(`Files changed in the exact review delta (${priorHead}..HEAD):`);
    expect(prompt).toContain('Missing null guard');
    expect(prompt).toContain('Added an early return.');
    expect(prompt).toContain('Do not start a full base-branch/PR rescan');
    expect(prompt).not.toContain('git diff origin/main...HEAD');
    expect(prompt).not.toContain('PLAYBOOK_CONTEXT');
    expect(prompt).not.toContain('PROJECT_CONTEXT');
    expect(prompt).not.toContain('SELF_REVIEW_BLIND_SPOTS');
  });

  it('accepts delta_base_sha from task context when the payload omits it', async () => {
    const priorHead = 'b'.repeat(40);
    await handleReview(
      'task-context-delta',
      { pr_number: 25, branch: 'feature/x', base_branch: 'main' },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 2, delta_base_sha: priorHead } } },
      'token', 'owner', 'repo', null, statusRef(),
    );

    expectCalledWith(indexMod.git, ['diff', '--name-status', `${priorHead}..HEAD`]);
    expect(lastReviewPrompt()).toContain(`git diff ${priorHead}..HEAD`);
  });

  it('pins the delta review base from live PR metadata without fetching the base branch', async () => {
    const priorHead = 'd'.repeat(40);
    const currentBase = 'e'.repeat(40);
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"verdict":"APPROVE","feedback":"fixed"}');
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ title: 'PR title', body: 'PR body', base: { sha: currentBase } }),
    });

    await handleReview(
      'task-delta-base-metadata',
      { pr_number: 251, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 2 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as { reviewed_base_sha?: string };
    expect(verdictJson.reviewed_base_sha).toBe(currentBase);
    expect(vi.mocked(indexMod.git).mock.calls.some(([args]) => args[0] === 'fetch')).toBe(false);
  });

  it('pins the delta review base from the backend review target', async () => {
    const priorHead = 'd'.repeat(40);
    const targetBase = 'c'.repeat(40);
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"verdict":"APPROVE","feedback":"fixed"}');
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ title: 'PR title', body: 'PR body' }),
    });

    await handleReview(
      'task-delta-base-target',
      { pr_number: 252, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      {
        ...REVIEW_TASK,
        context: {
          review_loop: {
            iteration: 2,
            review_target: { base_sha: targetBase, head_sha: 'f'.repeat(40) },
          },
        },
      },
      'token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as { reviewed_base_sha?: string };
    expect(verdictJson.reviewed_base_sha).toBe(targetBase);
    expect(vi.mocked(indexMod.git).mock.calls.some(([args]) => args[0] === 'fetch')).toBe(false);
  });

  it('overwrites model-authored SHA provenance with the mechanical review target', async () => {
    const priorHead = 'd'.repeat(40);
    const targetBase = 'c'.repeat(40);
    const targetHead = 'f'.repeat(40);
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(JSON.stringify({
      verdict: 'REQUEST_CHANGES',
      feedback: 'Needs a narrow fix.',
      issues: [{ severity: 'major', description: 'Bug', file: 'src/a.ts', line: 1 }],
      reviewed_base_sha: 'a'.repeat(40),
      reviewed_head_sha: 'b'.repeat(40),
    }));
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) =>
      args?.[0] === 'rev-parse' && args[1] === 'HEAD' ? targetHead : '',
    );

    await handleReview(
      'task-delta-authoritative-provenance',
      { pr_number: 253, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      {
        ...REVIEW_TASK,
        context: {
          review_loop: {
            iteration: 2,
            review_target: { base_sha: targetBase, head_sha: targetHead },
          },
        },
      },
      'token', 'owner', 'repo', null, statusRef(),
    );

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1];
    const verdictJson = JSON.parse(String(status.verdict_json)) as {
      reviewed_base_sha?: string;
      reviewed_head_sha?: string;
    };
    expect(verdictJson.reviewed_base_sha).toBe(targetBase);
    expect(verdictJson.reviewed_head_sha).toBe(targetHead);
  });

  it('does not fan out an exact delta review even when its changed-file list meets the full-review threshold', async () => {
    const priorHead = 'f'.repeat(40);
    const changedFiles = [
      'M\tagent/src/a.ts', 'M\tagent/src/b.ts', 'M\tagent/src/c.ts',
      'M\tadmin-ui/src/d.tsx', 'M\tadmin-ui/src/e.tsx',
      'M\tbackend/app/f.py', 'M\tbackend/app/g.py', 'M\tbackend/app/h.py',
    ].join('\n');
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) =>
      args?.[0] === 'diff' && args[1] === '--name-status' ? changedFiles : '',
    );
    expect(shouldFanOutReview(changedFiles.split('\n').map(line => line.slice(2)), BLUEPRINT_CONFIG)).toBe(true);

    await handleReview(
      'task-delta-no-fanout',
      { pr_number: 252, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      {
        ...REVIEW_TASK,
        context: { review_loop: { iteration: 2 } },
        project_metadata: { enable_parallel_review: true },
      },
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(lastReviewPrompt()).not.toContain('Parallel review fan-out');
  });

  it('does not download full-review viewport screenshots for an exact delta review', async () => {
    const priorHead = '9'.repeat(40);
    await handleReview(
      'task-delta-no-viewport-download',
      {
        pr_number: 253,
        branch: 'feature/x',
        base_branch: 'main',
        delta_base_sha: priorHead,
        viewport_shot_entries: [{ name: 'component-before.png' }],
        viewport_source_task_id: 'task-fix',
      },
      {
        ...REVIEW_TASK,
        context: { review_loop: { iteration: 2 } },
        project_metadata: { project_id: 'project-1' },
      },
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(viewportShotMod.downloadViewportShots).not.toHaveBeenCalled();
  });

  it('falls back to the established full PR review when the exact SHA is unavailable', async () => {
    const priorHead = 'c'.repeat(40);
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args?.[0] === 'cat-file') throw new Error('missing object');
      return '';
    });

    await handleReview(
      'task-unavailable-delta',
      { pr_number: 26, branch: 'feature/x', base_branch: 'main', delta_base_sha: priorHead },
      { ...REVIEW_TASK, context: { review_loop: { iteration: 2 } } },
      'token', 'owner', 'repo', null, statusRef(),
    );

    expectCalledWith(indexMod.git, ['fetch', 'origin', '+refs/heads/main:refs/remotes/origin/main', '--depth', '50']);
    expectCalledWith(indexMod.git, ['diff', '--name-status', 'origin/main...HEAD']);
    expect(lastReviewPrompt()).toContain('git diff origin/main...HEAD');
    expect(lastReviewPrompt()).not.toContain(`git diff ${priorHead}..HEAD`);
  });
});

// ── handleReview: completed-target deduplication ────────────────────────────
describe('handleReview — cached completed verdict replay', () => {
  const payload = { pr_number: 27, branch: 'feature/x', base_branch: 'main' };
  const reviewTarget = {
    project_id: 'project-1', pr_number: 27,
    base_sha: 'f'.repeat(40), head_sha: 'a'.repeat(40),
  };
  const task = {
    title: 'Review', description: '', project_context: {},
    context: { review_loop: { review_target: reviewTarget } },
  };

  it('replays a validated cached verdict without cloning or invoking the model', async () => {
    const cachedVerdict = JSON.stringify({
      verdict: 'APPROVE', feedback: 'Already reviewed.',
      reviewed_base_sha: reviewTarget.base_sha, reviewed_head_sha: reviewTarget.head_sha,
    });
    await handleReview(
      'task-cached-review',
      { ...payload, cached_verdict_json: cachedVerdict, cached_review_task_id: 'task-original-review' },
      task,
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(indexMod.runClaudeCode).not.toHaveBeenCalled();
    expectCalledWith(indexMod.persistAndPostStatus, 'task-cached-review', {
      task_id: 'task-cached-review',
      status: 'COMPLETED',
      logs: 'Reused completed review verdict from task task-original-review; no new model call was needed.',
      verdict_json: cachedVerdict,
    });
  });

  it('does not trust a malformed cached verdict and runs the normal review', async () => {
    await handleReview(
      'task-bad-cached-review',
      { ...payload, cached_verdict_json: '{"verdict":"COMMENT"}' },
      task,
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(indexMod.runClaudeCode).toHaveBeenCalled();
  });

  it('does not trust a cached verdict without both stamped target SHAs', async () => {
    const cachedVerdict = JSON.stringify({
      verdict: 'APPROVE', feedback: 'Missing head provenance.',
      reviewed_base_sha: reviewTarget.base_sha,
    });
    await handleReview(
      'task-cache-missing-provenance',
      { ...payload, cached_verdict_json: cachedVerdict },
      task,
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(indexMod.runClaudeCode).toHaveBeenCalled();
  });

  it('does not trust a cached verdict for a different base or head target', async () => {
    const cachedVerdict = JSON.stringify({
      verdict: 'APPROVE', feedback: 'Wrong target.',
      reviewed_base_sha: reviewTarget.base_sha, reviewed_head_sha: 'b'.repeat(40),
    });
    await handleReview(
      'task-cache-mismatched-provenance',
      { ...payload, cached_verdict_json: cachedVerdict },
      task,
      'token', 'owner', 'repo', null, statusRef(),
    );

    expect(indexMod.runClaudeCode).toHaveBeenCalled();
  });
});

// ── handleFix: fix-task self-review SR focus ─────────────────────────────────
describe('handleFix — fix-task SR focus block', () => {
  it('includes FIX_TASK_FOCUS content (regression_risk, scope-drift) in the fix prompt', async () => {
    await handleFix(
      'task-fix-sr-focus',
      { branch: 'feature/x', base_branch: 'main', pr_number: 200, prompt: 'address review feedback' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Self-review focus: fix tasks');
    expect(prompt).toContain('regression_risk');
    expect(prompt).toContain('scope-drift');
  });

  it('appends a repeating-issues verification directive when CRITICAL/MAJOR issues recur across iterations', async () => {
    const reviewHistory = [
      { iteration: 1, feedback: 'missing null check', issues: [{ severity: 'critical', description: 'null dereference in handler' }] },
      { iteration: 2, feedback: 'still present',       issues: [{ severity: 'critical', description: 'null dereference in handler' }] },
    ];
    await handleFix(
      'task-fix-sr-repeating',
      { branch: 'feature/x', base_branch: 'main', pr_number: 201, prompt: 'fix the null dereference' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: reviewHistory } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Repeating-issues verification');
    expect(prompt).toContain('null dereference in handler');
  });

  it('does not append the repeating-issues directive when there are no recurring CRITICAL/MAJOR issues', async () => {
    const reviewHistory = [
      { iteration: 1, feedback: 'missing test', issues: [{ severity: 'minor', description: 'add docstring' }] },
      { iteration: 2, feedback: 'addressed',    issues: [{ severity: 'minor', description: 'style nit' }] },
    ];
    await handleFix(
      'task-fix-sr-no-repeating',
      { branch: 'feature/x', base_branch: 'main', pr_number: 202, prompt: 'fix style' },
      { context: { scope: 'small', review_loop: { iteration: 2, review_history: reviewHistory } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('Repeating-issues verification');
  });

  it('renders the backend authoritative ledger without inferring closure from history', async () => {
    const reviewHistory = [
      {
        iteration: 1,
        feedback: 'null dereference',
        reviewed_head_sha: 'head-1',
        issues: [{ issue_id: 'BP-null', severity: 'major', description: 'null dereference in handler' }],
      },
    ];
    await handleFix(
      'task-fix-authoritative-history',
      { branch: 'feature/x', base_branch: 'main', pr_number: 203, prompt: 'finish the fix' },
      {
        context: {
          scope: 'small',
          review_loop: {
            iteration: 2,
            review_history: reviewHistory,
            issue_ledger: {
              open: [
                { issue_id: 'BP-null', severity: 'major', description: 'null dereference in handler', file: 'src/handler.ts', line: 42 },
                { issue_id: 'BP-race', severity: 'critical', description: 'race loses the update' },
              ],
            },
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Authoritative open blockers');
    expect(prompt).toContain('[MAJOR] [BP-null] [src/handler.ts:42] null dereference in handler');
    expect(prompt).toContain('[CRITICAL] [BP-race] race loses the update');
    expect(prompt).toContain('historical context only');
    expect(prompt).toContain('Address every blocker below');
    expect(prompt).toContain('fix agents cannot close blockers');
    expect(prompt).not.toContain('Re-raise an unresolved blocker');
    expect(prompt).not.toContain('(fixed in iteration');
    expect(prompt).not.toContain('likely resolved');
  });

  it('requires exact BP issue ids in structured fix responses', async () => {
    await handleFix(
      'task-fix-response-id',
      { branch: 'feature/x', base_branch: 'main', pr_number: 204, prompt: 'fix issue [BP-deadbeef1234]' },
      { context: { scope: 'small', review_loop: { iteration: 1 } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('copy the exact BP-... id shown with the reviewer issue');
    expect(prompt).toContain('Do not hash, recompute, or invent an id');
    expect(prompt).not.toContain('sha1 of the issue');
  });
});

// ── shouldFanOutReview threshold helper ──────────────────────────────────────
describe('shouldFanOutReview — fan-out gating helper', () => {
  it('returns false for trivial diff (1 file, 1 dir)', () => {
    expect(shouldFanOutReview(['backend/app/foo.py'], BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns false for single-module rollup (10 files, 1 dir)', () => {
    const files = Array.from({ length: 10 }, (_, i) => `backend/app/m${i}.py`);
    expect(shouldFanOutReview(files, BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns true for multi-module rollup (8 files, 3 dirs)', () => {
    const files = [
      'agent/src/a.ts', 'agent/src/b.ts', 'agent/src/c.ts',
      'admin-ui/src/d.tsx', 'admin-ui/src/e.tsx',
      'backend/app/f.py', 'backend/app/g.py', 'backend/app/h.py',
    ];
    expect(shouldFanOutReview(files, BLUEPRINT_CONFIG)).toBe(true);
  });

  it('returns false when buildConfig is missing (conservative default)', () => {
    const files = Array.from({ length: 8 }, (_, i) => `pkg${i}/src/m.ts`);
    expect(shouldFanOutReview(files, undefined)).toBe(false);
  });

  it('ignores test files when counting non-test threshold', () => {
    // 3 non-test files (below threshold of 4) across 2 dirs → false even with test padding
    const files = [
      'agent/src/a.ts',
      'agent/src/a.test.ts',
      'admin-ui/src/b.tsx',
      'admin-ui/src/b.test.tsx',
      'backend/app/c.py',
      'backend/testing/test_c.py',
    ];
    expect(shouldFanOutReview(files, BLUEPRINT_CONFIG)).toBe(false);
  });
});

// ── handleReview: parallel-review fan-out injection ──────────────────────────
describe('handleReview — parallel-review fan-out gating', () => {
  const PARALLEL_PAYLOAD = { pr_number: 700, branch: 'feat/rollup', base_branch: 'main', original_prompt: '' };
  const BIG_DIFF = (
    'M\tagent/src/a.ts\n' +
    'M\tagent/src/b.ts\n' +
    'M\tadmin-ui/src/c.tsx\n' +
    'M\tadmin-ui/src/d.tsx\n' +
    'M\tbackend/app/e.py'
  );

  function mockBigCrossModuleDiff(): void {
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) return BIG_DIFF;
      return '';
    });
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
  }

  function lastReviewPrompt(): string {
    const calls = vi.mocked(indexMod.runClaudeCode).mock.calls;
    if (calls.length === 0) throw new Error('runClaudeCode was not invoked');
    return calls[0][0];
  }

  it('injects PARALLEL_REVIEW_GUIDANCE when the project flag is on and threshold is met', async () => {
    mockBigCrossModuleDiff();
    await handleReview(
      'task-parallel-on',
      PARALLEL_PAYLOAD,
      {
        context: { scope: 'large' },
        project_metadata: { enable_parallel_review: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).toContain('Parallel review fan-out');
  });

  it('does NOT inject parallel guidance when the project flag is off (even for big multi-module diffs)', async () => {
    mockBigCrossModuleDiff();
    await handleReview(
      'task-parallel-off',
      PARALLEL_PAYLOAD,
      {
        context: { scope: 'large' },
        project_metadata: { enable_parallel_review: false },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain('Parallel review fan-out');
  });

  it('does NOT inject parallel guidance when project_metadata is absent (backward compatibility)', async () => {
    mockBigCrossModuleDiff();
    await handleReview(
      'task-parallel-absent',
      PARALLEL_PAYLOAD,
      { context: { scope: 'large' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain('Parallel review fan-out');
  });

  it('does NOT inject parallel guidance when the helper returns false (trivial diff) even with flag on', async () => {
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tbackend/app/only.py';
      }
      return '';
    });
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
    await handleReview(
      'task-parallel-trivial',
      PARALLEL_PAYLOAD,
      {
        context: { scope: 'small' },
        project_metadata: { enable_parallel_review: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(lastReviewPrompt()).not.toContain('Parallel review fan-out');
  });

  it('prompt fingerprint changes when parallel guidance is enabled', () => {
    const baseFp = computeReviewPromptFingerprint(BLUEPRINT_CONFIG);
    const parallelFp = computeReviewPromptFingerprint(BLUEPRINT_CONFIG, { includeParallelReviewGuidance: true });
    expect(parallelFp).not.toBe(baseFp);
  });

  it('records review_was_parallelized=true in the verdict JSON when fan-out was injected', async () => {
    mockBigCrossModuleDiff();
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"verdict":"APPROVE","feedback":"ok"}');
    await handleReview(
      'task-parallel-telemetry',
      PARALLEL_PAYLOAD,
      {
        context: { scope: 'large' },
        project_metadata: { enable_parallel_review: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const lastCall = vi.mocked(indexMod.persistAndPostStatus).mock.calls.at(-1);
    if (!lastCall) throw new Error('persistAndPostStatus was not invoked');
    const payload = lastCall[1] as { verdict_json?: string };
    expect(payload.verdict_json).toBeDefined();
    const parsed = JSON.parse(defined(payload.verdict_json)) as Record<string, unknown>;
    expect(parsed.review_was_parallelized).toBe(true);
  });

  it('does NOT add review_was_parallelized when fan-out was not injected', async () => {
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-status')) {
        return 'M\tbackend/app/only.py';
      }
      return '';
    });
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"verdict":"APPROVE","feedback":"ok"}');
    await handleReview(
      'task-parallel-no-telemetry',
      PARALLEL_PAYLOAD,
      {
        context: { scope: 'small' },
        project_metadata: { enable_parallel_review: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const lastCall = vi.mocked(indexMod.persistAndPostStatus).mock.calls.at(-1);
    if (!lastCall) throw new Error('persistAndPostStatus was not invoked');
    const payload = lastCall[1] as { verdict_json?: string };
    expect(payload.verdict_json).toBeDefined();
    const parsed = JSON.parse(defined(payload.verdict_json)) as Record<string, unknown>;
    expect(parsed.review_was_parallelized).toBeUndefined();
  });
});

// ── shouldFanOutFix threshold helper ─────────────────────────────────────────
describe('shouldFanOutFix — fan-out gating helper', () => {
  it('returns false for trivial diff (1 file)', () => {
    expect(shouldFanOutFix(['backend/app/foo.py'], 0, [], BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns false for single-module diff below file threshold', () => {
    const files = ['backend/app/a.py', 'backend/app/b.py', 'backend/app/c.py'];
    expect(shouldFanOutFix(files, 0, [], BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns true for multi-module diff (4+ non-test files, 2+ dirs)', () => {
    const files = [
      'agent/src/a.ts', 'agent/src/b.ts',
      'backend/app/c.py', 'backend/app/d.py',
    ];
    expect(shouldFanOutFix(files, 0, [], BLUEPRINT_CONFIG)).toBe(true);
  });

  it('returns true when reviewer issues cross threshold (6 issues, 2 files)', () => {
    expect(shouldFanOutFix([], 6, ['file_a.ts', 'file_b.ts'], BLUEPRINT_CONFIG)).toBe(true);
  });

  it('returns false when issue count meets threshold but only 1 file', () => {
    expect(shouldFanOutFix([], 6, ['file_a.ts'], BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns false when issue file count meets threshold but issue count is below', () => {
    expect(shouldFanOutFix([], 5, ['file_a.ts', 'file_b.ts'], BLUEPRINT_CONFIG)).toBe(false);
  });

  it('returns false when buildConfig is missing and diff is trivial', () => {
    const files = ['agent/src/a.ts', 'backend/app/b.py'];
    expect(shouldFanOutFix(files, 0, [], undefined)).toBe(false);
  });

  it('ignores test files when counting non-test threshold', () => {
    // 3 non-test files across 2 dirs but below minNonTestFiles=4 → false
    const files = [
      'agent/src/a.ts',
      'agent/src/a.test.ts',
      'backend/app/b.py',
      'backend/testing/test_b.py',
      'admin-ui/src/c.tsx',
    ];
    expect(shouldFanOutFix(files, 0, [], BLUEPRINT_CONFIG)).toBe(false);
  });
});

// ── handleFix: parallel-fix fan-out injection ─────────────────────────────────
describe('handleFix — parallel-fix fan-out gating', () => {
  const FIX_PAYLOAD = {
    pr_number: 800,
    branch: 'fix/rollup',
    base_branch: 'main',
    prompt: 'Fix the issues',
    original_prompt: 'Original task',
  };
  const BIG_FIX_DIFF = [
    'agent/src/a.ts',
    'agent/src/b.ts',
    'backend/app/c.py',
    'backend/app/d.py',
  ].join('\n');

  function mockBigFixDiff(): void {
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-only')) return BIG_FIX_DIFF;
      return '';
    });
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
  }

  it('injects PARALLEL_FIX_GUIDANCE when flag is on and multi-module diff threshold is met', async () => {
    mockBigFixDiff();
    await handleFix(
      'task-fix-parallel-on',
      FIX_PAYLOAD,
      {
        context: { scope: 'large', review_loop: { review_history: [] } },
        project_metadata: { enable_parallel_fix: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).toContain('Parallel fix investigation');
  });

  it('injects PARALLEL_FIX_GUIDANCE when flag is on and reviewer issue count meets threshold', async () => {
    vi.mocked(indexMod.git).mockReturnValue('');
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
    const issues = Array.from({ length: 6 }, (_, i) => ({
      severity: 'major',
      description: `Issue ${i}`,
      file: i < 3 ? 'backend/app/foo.py' : 'agent/src/bar.ts',
    }));
    await handleFix(
      'task-fix-parallel-issues',
      FIX_PAYLOAD,
      {
        context: {
          scope: 'large',
          review_loop: { review_history: [{ iteration: 1, feedback: 'feedback', issues }] },
        },
        project_metadata: { enable_parallel_fix: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).toContain('Parallel fix investigation');
  });

  it('does NOT inject PARALLEL_FIX_GUIDANCE when flag is off', async () => {
    mockBigFixDiff();
    await handleFix(
      'task-fix-parallel-off',
      FIX_PAYLOAD,
      {
        context: { scope: 'large', review_loop: { review_history: [] } },
        project_metadata: { enable_parallel_fix: false },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).not.toContain('Parallel fix investigation');
  });

  it('does NOT inject PARALLEL_FIX_GUIDANCE when project_metadata is absent (backward compat)', async () => {
    mockBigFixDiff();
    await handleFix(
      'task-fix-parallel-absent',
      FIX_PAYLOAD,
      { context: { scope: 'large', review_loop: { review_history: [] } } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).not.toContain('Parallel fix investigation');
  });

  it('does NOT inject PARALLEL_FIX_GUIDANCE when diff is trivial (single-module)', async () => {
    vi.mocked(indexMod.git).mockImplementation((args?: string[]) => {
      if (args && args[0] === 'diff' && args.includes('--name-only')) {
        return 'backend/app/only.py';
      }
      return '';
    });
    vi.mocked(resolveBuildConfig).mockReturnValue(BLUEPRINT_CONFIG);
    await handleFix(
      'task-fix-parallel-trivial',
      FIX_PAYLOAD,
      {
        context: { scope: 'small', review_loop: { review_history: [] } },
        project_metadata: { enable_parallel_fix: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(capturedFixPrompt()).not.toContain('Parallel fix investigation');
  });

  it('prompt fingerprint changes when parallel fix guidance is enabled', () => {
    const baseFp = computePromptFingerprint('fix', BLUEPRINT_CONFIG);
    const parallelFp = computePromptFingerprint('fix', BLUEPRINT_CONFIG, { includeParallelFixGuidance: true });
    expect(parallelFp).not.toBe(baseFp);
  });

  it('writes fix_parallelized telemetry file when fan-out is injected', async () => {
    mockBigFixDiff();
    await handleFix(
      'task-fix-telemetry',
      FIX_PAYLOAD,
      {
        context: { scope: 'large', review_loop: { review_history: [] } },
        project_metadata: { enable_parallel_fix: true },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/fix_parallelized_task-fix-telemetry.json',
      expect.stringContaining('"fix_was_parallelized":true'),
      'utf-8',
    );
  });
});

// ── Read-only ./fetch provisioning + guidance ─────────────────────────────────
describe('mid-run ./fetch provisioning for review/fix', () => {
  const reviewPayload = { pr_number: 5, branch: 'feat/y', base_branch: 'main', original_prompt: '' };
  const fixPayload = { branch: 'feat/y', base_branch: 'main', pr_number: 5, prompt: 'address review' };
  const fixContext = { context: { scope: 'small', review_loop: { iteration: 1, review_history: [] } } };

  it('handleReview advertises ./fetch guidance and provisions the writer when project_id is set', async () => {
    await handleReview(
      'task-review-fetch', reviewPayload,
      { context: { scope: 'medium' }, project_metadata: { project_id: 'p-rev' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('MID-RUN FETCH (./fetch)');
    expect(prompt).toContain('./fetch direction [id]');
    expect(prompt).toContain('./fetch section <key>');
    expect(prompt).toContain('./fetch playbook <key>');
    expect(prompt).not.toContain('./fetch problem-model');
    expectCalledWith(writeFetchHelper,
      'task-review-fetch', 'p-rev', expect.anything(), { verbs: PLAN_FETCH_VERBS },
    );
  });

  it('handleReview omits ./fetch guidance and does not provision when project_id is absent', async () => {
    await handleReview(
      'task-review-nofetch', reviewPayload, { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('MID-RUN FETCH (./fetch)');
    expect(vi.mocked(writeFetchHelper)).not.toHaveBeenCalled();
  });

  it('handleFix advertises ./fetch guidance and provisions the writer when project_id is set', async () => {
    await handleFix(
      'task-fix-fetch', fixPayload,
      { ...fixContext, project_metadata: { project_id: 'p-fix' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('MID-RUN FETCH (./fetch)');
    expectCalledWith(writeFetchHelper,
      'task-fix-fetch', 'p-fix', expect.anything(), { verbs: PLAN_FETCH_VERBS },
    );
  });

  it('handleFix omits ./fetch guidance and does not provision when project_id is absent', async () => {
    await handleFix(
      'task-fix-nofetch', fixPayload, fixContext,
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('MID-RUN FETCH (./fetch)');
    expect(vi.mocked(writeFetchHelper)).not.toHaveBeenCalled();
  });
});

// ── handleFix: authoritative issue source ─────────────────────────────────────
describe('handleFix — authoritative issue source', () => {
  // Use a medium-scope iteration in convergence mode (iteration * 2 >= maxIterations).
  // For medium scope, maxIterations = 12. With iteration = 7: 7*2=14 >= 12 ✓ and 7 < 12 ✓.
  // Convergence mode uses latestIssues for file-scope guidance and CRITICAL/MAJOR counts —
  // the primary observable place where survivingIssues influences the prompt text.
  const rawHistoryIssue = { severity: 'major', description: 'Old issue from raw history', file: 'src/old.ts', line: 5 };
  const authoritativeIssue = { issue_id: 'BP-new', severity: 'critical', description: 'Binding open blocker', file: 'src/new.ts', line: 22 };
  const staleHistoryIssue = { severity: 'major', description: 'Resolved issue retained in history', file: 'src/resolved.ts', line: 9 };

  const reviewHistory = [{
    iteration: 1,
    feedback: 'primary review feedback',
    issues: [rawHistoryIssue, staleHistoryIssue],
  }];

  // Convergence-mode context: iteration 7 of 12 (7*2=14 >= 12).
  const convergenceContext = {
    context: {
      scope: 'medium',
      review_loop: { iteration: 7, review_history: reviewHistory },
    },
  };

  it('handleFix uses the authoritative open ledger instead of stale history or payload compatibility fields', async () => {
    await handleFix(
      'task-authoritative-ledger',
      {
        branch: 'feat/challenge',
        base_branch: 'main',
        pr_number: 77,
        prompt: 'Fix the authoritative blockers',
      },
      {
        context: {
          scope: 'medium',
          review_loop: {
            iteration: 7,
            review_history: reviewHistory,
            issue_ledger: { open: [authoritativeIssue] },
          },
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Address exactly 1 CRITICAL and 0 MAJOR');
    expect(prompt).toContain('src/new.ts');
    expect(prompt).not.toContain('Files you may modify: src/resolved.ts');
  });

  it('handleFix falls back to latest review-history issues when no authoritative ledger exists', async () => {
    await handleFix(
      'task-no-surviving',
      { branch: 'feat/challenge', base_branch: 'main', pr_number: 79, prompt: 'Fix review issues' },
      convergenceContext,
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('Address exactly 0 CRITICAL and 2 MAJOR');
    expect(prompt).toContain('src/old.ts');
    expect(prompt).toContain('src/resolved.ts');
  });
});

// ── handleReview: domain_overview reaches review prompt ─────────────────────
describe('handleReview — domain overview', () => {
  it('renders ## Domain Knowledge in review prompt when task.domain_overview is present', async () => {
    await handleReview(
      'task-review-domain',
      { pr_number: 1, branch: 'feat/x', base_branch: 'main', original_prompt: '' },
      {
        context: { scope: 'medium' },
        domain_overview: {
          summary: 'A healthcare scheduling platform.',
          body: 'Manages appointments, providers, and patient records.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('## Domain Knowledge');
    expect(prompt).toContain('A healthcare scheduling platform.');
    expect(prompt).toContain('Manages appointments, providers, and patient records.');
  });

  it('omits ## Domain Knowledge from review prompt when domain_overview is absent', async () => {
    await handleReview(
      'task-review-no-domain',
      { pr_number: 2, branch: 'feat/y', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'medium' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).not.toContain('## Domain Knowledge');
  });
});

// ── handleFix: domain_overview reaches fix prompt ────────────────────────────
describe('handleFix — domain overview', () => {
  it('renders ## Domain Knowledge in fix prompt when task.domain_overview is present', async () => {
    await handleFix(
      'task-fix-domain',
      { branch: 'feat/x', base_branch: 'main', pr_number: 1, prompt: 'address review' },
      {
        context: { scope: 'small' },
        domain_overview: {
          summary: 'A logistics routing service.',
          body: 'Core entities: shipments, routes, depots.',
        },
      },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).toContain('## Domain Knowledge');
    expect(prompt).toContain('A logistics routing service.');
    expect(prompt).toContain('Core entities: shipments, routes, depots.');
  });

  it('omits ## Domain Knowledge from fix prompt when domain_overview is absent', async () => {
    await handleFix(
      'task-fix-no-domain',
      { branch: 'feat/y', base_branch: 'main', pr_number: 2, prompt: 'address review' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );
    const prompt = capturedFixPrompt();
    expect(prompt).not.toContain('## Domain Knowledge');
  });
});

// ── handleReview: PR metadata fetch aborts on timeout ────────────────────────
describe('handleReview — pr metadata fetch aborts on timeout', () => {
  it('aborts the PR metadata fetch after the timeout fires and still completes the review', async () => {
    // Simulate a non-resolving fetch (hangs indefinitely without a timeout).
    // The AbortController in handleReview should abort it after 25s, which
    // causes the fetch promise to reject with an AbortError — caught by the
    // surrounding try/catch, so handleReview still proceeds without metadata.
    let capturedSignal: AbortSignal | undefined;
    global.fetch = vi.fn().mockImplementation((_url: unknown, opts?: { signal?: AbortSignal }) => {
      capturedSignal = opts?.signal;
      // Return a promise that only resolves when the signal aborts.
      return new Promise<never>((_resolve, reject) => {
        if (opts?.signal) {
          opts.signal.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }
      });
    });

    // Use fake timers so we can fire the 25s timeout without waiting.
    vi.useFakeTimers();

    const reviewPromise = handleReview(
      'task-meta-timeout',
      { pr_number: 999, branch: 'feat/timeout', base_branch: 'main', original_prompt: '' },
      { context: { scope: 'small' } },
      'gh-token', 'owner', 'repo', null, statusRef(),
    );

    // Advance time past the 25s PR metadata timeout.
    await vi.advanceTimersByTimeAsync(26_000);

    vi.useRealTimers();

    // handleReview must complete (not hang) even though the fetch never resolved.
    await reviewPromise;

    // The signal was wired up and was aborted by the timeout.
    expect(capturedSignal).toBeDefined();
    expect(capturedSignal?.aborted).toBe(true);

    // runClaudeCode was still called — the review proceeded despite missing metadata.
    expect(vi.mocked(indexMod.runClaudeCode)).toHaveBeenCalled();
  });
});
