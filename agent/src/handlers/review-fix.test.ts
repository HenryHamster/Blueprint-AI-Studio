import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
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
  setupWorkspace: vi.fn(),
  pushAndCreatePR: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  lintFixLoop: vi.fn(),
  testFixLoop: vi.fn().mockResolvedValue({ passed: true, testFailuresDetected: false, failureSummary: '' }),
  captureTestBaseline: vi.fn().mockResolvedValue(null),
  runProjectLintChecks: vi.fn(),
  cloneWithRetry: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../prompt.js', () => ({
  getTaskTypeHints: vi.fn().mockReturnValue({}),
  PLAYBOOK_GUIDANCE: '',
  FIRST_TASK_GUIDANCE: '',
  DEFAULT_TOOL_GUIDANCE: '',
  readClaudeMd: vi.fn().mockReturnValue(''),
  formatProjectContext: vi.fn().mockReturnValue(''),
  configureAgentPermissions: vi.fn().mockReturnValue([]),
  writePlaybookHelper: vi.fn(),
  writeMessagesHelper: vi.fn(),
  formatPlaybook: vi.fn().mockReturnValue(''),
  buildRetryContext: vi.fn().mockReturnValue(''),
  discoverRelatedTests: vi.fn().mockReturnValue(''),
}));

vi.mock('../build-config.js', () => ({
  resolveBuildConfig: vi.fn().mockReturnValue({}),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(''),
}));

import fs from 'fs';
import { execFileSync } from 'child_process';
import * as indexMod from '../index.js';
import { computeMaxFixIterations, handleReview, handleFix, REVIEW_FEEDBACK_HISTORY_CHARS } from './review-fix.js';

const statusRef = () => ({ value: '' });

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
    json: async () => ({ title: 'PR title', body: 'PR body' }),
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

  it('does not throw on unusual (but typed) context shapes', () => {
    // Empty object is the realistic edge; the guard coerces to ctx={}.
    expect(() => computeMaxFixIterations({ context: {} })).not.toThrow();
    expect(() => computeMaxFixIterations({})).not.toThrow();
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
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
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
    const prompt = vi.mocked(indexMod.runClaudeCode).mock.calls[0][0];
    expect(prompt).toContain('[MAJOR] [a.ts:10] real problem');
    expect(prompt).not.toMatch(/- Iteration 1: x+/);
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
      '{"verdict":"APPROVE","feedback":"looks good"}'
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
    const status = call[1] as Record<string, unknown>;
    expect(status.status).toBe('COMPLETED');
    expect(status.verdict_json).toBe('{"verdict":"APPROVE","feedback":"looks good"}');
    expect(String(status.logs)).toContain('"verdict":"APPROVE"');
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

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
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

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(status.verdict_json).toBeUndefined();
    expect(String(status.logs)).toBe('last');
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

    const status = vi.mocked(indexMod.persistAndPostStatus).mock.calls[0][1] as Record<string, unknown>;
    expect(status.status).toBe('FAILED');
    expect(status.failure_category).toBe('claude_code_failed');
    expect(status.artifact_type).toBe('ERROR_LOG');
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

    // Claude was invoked at least once with the marker-enriched prompt.
    const firstCall = vi.mocked(indexMod.runClaudeCode).mock.calls[0];
    expect(firstCall, 'runClaudeCode should have been called').toBeTruthy();
    const prompt = String(firstCall[0]);
    expect(prompt).toContain('CONFLICT MARKERS FOUND');
    expect(prompt).toContain('backend/app/foo.py');
    expect(prompt).toContain('agent/src/bar.ts');
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

    const firstCall = vi.mocked(indexMod.runClaudeCode).mock.calls[0];
    expect(firstCall, 'runClaudeCode should have been called').toBeTruthy();
    const prompt = String(firstCall[0]);
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

    // The obsolete path short-circuits before runClaudeCode.
    expect(vi.mocked(indexMod.runClaudeCode)).not.toHaveBeenCalled();

    const statusCalls = vi.mocked(indexMod.persistAndPostStatus).mock.calls;
    expect(statusCalls.length).toBeGreaterThanOrEqual(1);
    const status = statusCalls[0][1] as Record<string, unknown>;
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

    const prompt = String(vi.mocked(indexMod.runClaudeCode).mock.calls[0][0]);
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

    const prompt = String(vi.mocked(indexMod.runClaudeCode).mock.calls[0][0]);
    expect(prompt).not.toContain('Stale-premise notice');
  });
});
