import { describe, it, expect, vi } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  strVal: (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback),
  sanitize: (s: string) => s,
  sendLog: vi.fn(),
  log: vi.fn(),
  getAgentToken: vi.fn().mockReturnValue(''),
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  readHandlerOutput: vi.fn(),
  validateBranchName: vi.fn((b: string, _label: string) => b),
  git: vi.fn().mockReturnValue(''),
  gitAsync: vi.fn().mockResolvedValue(''),
  setupWorkspace: vi.fn(),
  pushAndCreatePR: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  lintFixLoop: vi.fn(),
  testFixLoop: vi.fn(),
  captureTestBaseline: vi.fn(),
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
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
}));

import { computeMaxFixIterations } from './review-fix.js';

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
    // Backend: ctx.get('scope', 'medium') → 'medium' → limit=12.
    // A divergence here would let the agent claim "FINAL attempt" at iter 16
    // while the backend still has iters 13–16 queued.
    expect(computeMaxFixIterations({ context: {} })).toBe(12);
    expect(computeMaxFixIterations({})).toBe(12);
  });
});
